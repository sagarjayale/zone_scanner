"""Paper trading. No order is ever sent to Upstox: fills are simulated against live prices,
with stored 5-minute candles as a safety net for anything the price polling missed.
The fill rules are the ones the backtest uses (app/sim/fills.py).

Trailing stop (Settings, off by default): a position keeps the trailing settings it was filled with. After each bar
close the stop is worked out again from the closed candles of its trailing timeframe (sim/trailing.py, the same rules
as the scan-date check) and moved when that is better. A position that ends on a moved stop closes as "trail"."""
import math
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import MODES, SESSION_OPEN
from ..data.resample import close_time
from ..data.sync import load_candles, now_ist
from ..engine import costs as charges
from ..engine.link import ACTIVE
from ..engine.plan import current_plan
from ..models import Instrument, PaperOrder, PaperTrade, TradePlan, Zone
from . import fills, trailing


GAP_WINDOW_END = time(9, 20)      # kept for older imports: a live limit now always fills at the real price (fills.limit_fill_price)


def live_orders(db: Session, key: str | None = None) -> list[PaperOrder]:
    q = select(PaperOrder).where(PaperOrder.status == "pending", PaperOrder.archived.isnot(True))
    return list(db.scalars(q.where(PaperOrder.instrument_key == key) if key else q).all())


def open_trades(db: Session, key: str | None = None) -> list[PaperTrade]:
    q = select(PaperTrade).where(PaperTrade.exit_ts.is_(None), PaperTrade.archived.isnot(True))
    return list(db.scalars(q.where(PaperTrade.instrument_key == key) if key else q).all())


def quantity(db: Session, key: str, entry: float, stop: float) -> int:
    """Size the position so that a stop-out loses the chosen percentage of capital."""
    risk_amount = float(settings_store.get(db, "paper_capital")) * float(settings_store.get(db, "paper_risk_pct")) / 100.0
    per_unit = abs(entry - stop)
    qty = math.floor(risk_amount / per_unit) if per_unit > 0 else 0
    inst = db.get(Instrument, key)
    lot = inst.lot_size if inst and inst.segment == "NSE_FO" and inst.lot_size else 1
    return max(lot, (qty // lot) * lot)


def place(db: Session, zone_id: int, now: datetime, mode: str | None = None) -> PaperOrder:
    """Create a pending paper order from a zone's plan. Entry type 1 is a limit at the proximal line. Entry types 2
    and 3 wait for the candles to confirm, and fill at the entry the scan records. One live order or position per zone."""
    z = db.get(Zone, zone_id)
    p = current_plan(db, zone_id, mode) if z else None
    if not z or not p:
        raise ValueError("This zone has no trade plan.")
    if z.status not in ACTIVE or p.status != "pending":
        raise ValueError(f"This setup can no longer be traded: the zone is {z.status} and the plan is {p.status}.")
    taken = db.scalars(select(PaperOrder).where(PaperOrder.zone_id == zone_id, PaperOrder.archived.isnot(True),
                                                PaperOrder.status.in_(("pending", "filled")))).first()
    if taken:
        raise ValueError("A paper order already exists for this zone.")
    order = PaperOrder(plan_id=p.id, zone_id=z.id, instrument_key=z.instrument_key, timeframe=z.timeframe, side=z.side,
                       quantity=quantity(db, z.instrument_key, p.entry, p.stop), entry=p.entry, stop=p.stop, target=p.target,
                       status="pending", created_at=now, archived=False, entry_type=p.entry_type or "TYPE_1")
    db.add(order)
    db.commit()
    return order


def cancel(db: Session, order_id: int, note: str = "cancelled by you") -> PaperOrder:
    order = db.get(PaperOrder, order_id)
    if not order or order.status != "pending":
        raise ValueError("No pending order with that id.")
    order.status, order.note = "cancelled", note
    db.commit()
    return order


def _fill(db: Session, order: PaperOrder, ts: datetime, price: float | None = None) -> PaperTrade:
    """Turn an order into a position at `price` (the order's entry unless given: a gap at the open fills at the open)."""
    order.status = "filled"
    fill = float(order.entry if price is None else price)
    trade = PaperTrade(order_id=order.id, zone_id=order.zone_id, instrument_key=order.instrument_key, timeframe=order.timeframe,
                       side=order.side, quantity=order.quantity, fill_price=fill, fill_ts=ts, stop=order.stop,
                       target=order.target, risk=abs(fill - order.stop) or abs(order.entry - order.stop), kind="zone", archived=False, history=[],
                       trail=_trail_start(db, order))
    db.add(trade)
    db.flush()
    return trade


LIVE_TF = "5m"     # trail_tf "live": the stored candles that back up the live prices


def _trail_tf(db: Session, opts: dict, ltf: str, plan_id: int | None) -> str:
    """The candles a trailing stop is worked out on: 5 minutes for "live" (plus each live price), the trade's own
    timeframe for "ltf", the mode's middle timeframe for "itf"."""
    if opts["trail_tf"] == "live":
        return LIVE_TF
    if opts["trail_tf"] == "itf":
        plan = db.get(TradePlan, plan_id) if plan_id else None
        if plan is not None and plan.mode in MODES:
            return MODES[plan.mode][1]
    return ltf


def _trail_start(db: Session, order: PaperOrder) -> dict | None:
    """The trailing stop settings a new position keeps for its whole life (None when trailing is off)."""
    opts = trailing.options({k: settings_store.get(db, k) for k in trailing.OPTION_KEYS})
    if not trailing.is_on(opts):
        return None
    tf = _trail_tf(db, opts, order.timeframe, order.plan_id)
    return {**opts, "tf": tf, "first_stop": float(order.stop), "first_target": float(order.target), "moves": 0,
            "no_target": trailing.no_target_from_fill(opts)}


def apply_trailing(db: Session, trade_id: int, now: datetime) -> PaperTrade:
    """Give an open zone position the trailing stop of the Settings page now (for a position filled while trailing was
    off, or to switch it to the current settings). It starts from the position's current stop and target, then catches
    up on the candles that have closed since the fill, so the stop is where it would be had trailing been on."""
    trade = db.get(PaperTrade, trade_id)
    if not trade or trade.exit_ts is not None or trade.kind == "option":
        raise ValueError("No open zone position with that id.")
    opts = trailing.options({k: settings_store.get(db, k) for k in trailing.OPTION_KEYS})
    if not trailing.is_on(opts):
        raise ValueError("Trailing is off in Settings. Choose a method there first (for example Lock), then apply it.")
    sign = 1 if trade.side == "demand" else -1
    if (trade.target - trade.fill_price) * sign <= 0:
        raise ValueError("The target is not beyond the entry, so there are no steps to lock.")
    order = db.get(PaperOrder, trade.order_id) if trade.order_id else None
    tf = _trail_tf(db, opts, trade.timeframe, order.plan_id if order is not None else None)
    trade.trail = {**opts, "tf": tf, "first_stop": float(trade.stop), "first_target": float(trade.target), "moves": 0,
                   "no_target": trailing.no_target_from_fill(opts)}
    log = list(trade.history or [])
    log.append({"time": now.isoformat(timespec="seconds"), "field": "trailing", "old": None, "new": None,
                "note": f"trailing applied by you: {opts['trail_method']}" + (f" {opts['trail_lock_pct']:g}%, gap {opts['trail_lock_gap_pct']:g}%" if opts["trail_method"] == "lock" else "")})
    trade.history = log
    update_trailing(db, trade, now)
    db.commit()
    return trade


def live_target(trade: PaperTrade) -> float:
    """The target that closes the position now: out of reach once a trailing stop that drops the target has started."""
    if trade.trail and trade.trail.get("no_target"):
        return math.inf if trade.side == "demand" else -math.inf
    return trade.target


def _stop_reason(trade: PaperTrade) -> str:
    return "trail" if trade.trail and trade.trail.get("moves") else "stop"


def _tick(db: Session, key: str) -> float:
    """The instrument's tick size in rupees, as stored (NSE equity 0.05 until the instrument file gives its own)."""
    inst = db.get(Instrument, key)
    v = float(inst.tick_size) if inst is not None and inst.tick_size else 0.0
    return v if v > 0 else 0.05


def _to_tick(price: float, tick: float) -> float:
    return round(round(float(price) / tick) * tick, 2)


def update_trailing(db: Session, trade: PaperTrade, now: datetime) -> list[dict]:
    """Move the stop of an open position with a trailing stop, from the closed candles of its trailing timeframe
    since the fill. Worked out from the start each time, so a restart or a late candle gives the same stop."""
    st = trade.trail
    if not trailing.is_on(st) or trade.exit_ts is not None or trade.kind == "option":
        return []
    tf = st.get("tf") or trade.timeframe
    before = load_candles(db, trade.instrument_key, tf, until=trade.fill_ts, last=25)
    before = [(r.high, r.low, r.close) for r in before.itertuples() if close_time(r.ts.to_pydatetime(), tf) <= trade.fill_ts]
    after = load_candles(db, trade.instrument_key, tf, since=trade.fill_ts)
    value = float(trade.fill_price) * max(1, int(trade.quantity or 1))
    cost_frac = charges.round_trip(value, value, settings_store.thresholds(db, tf)) / value if value > 0 else 0.0
    t = trailing.Trailer(trade.side, trade.fill_price, st["first_stop"], st, before, st.get("first_target"), cost_frac)
    for r in after.itertuples():
        if close_time(r.ts.to_pydatetime(), tf) <= now:
            t.close_bar(r.high, r.low, r.close)
    if st.get("trail_tf") == "live" and st.get("live_best") is not None:
        t.see_price(st["live_best"])          # the best live price since the fill: the stop moves without a candle
    long = trade.side == "demand"
    state = {**st, "moves": t.moves, "best": t.best, "best_r": round(t.best_r, 3), "active": t.active, "why": t.why,
             "no_target": t.target_removed}
    events, log = [], list(trade.history or [])
    tick = _tick(db, trade.instrument_key)
    new_stop = _to_tick(t.stop, tick)
    better = (new_stop > trade.stop + 1e-9) if long else (new_stop < trade.stop - 1e-9)
    if better:
        log.append({"time": now.isoformat(timespec="seconds"), "field": "stop", "old": trade.stop, "new": new_stop,
                    "note": f"trailing stop ({st['trail_method']}): {t.why}, best +{t.best_r:.2f} R"})
        trade.stop = new_stop
        events.append(_event("stop moved", trade, trade.stop, now))
    if t.method == "lock" and t.target is not None and t.target_moves and abs(t.target - trade.target) > 1e-9:
        log.append({"time": now.isoformat(timespec="seconds"), "field": "target", "old": trade.target, "new": _to_tick(t.target, tick),
                    "note": "lock: the target moved forward one step"})
        trade.target = _to_tick(t.target, tick)
    if state["no_target"] and not st.get("no_target"):
        log.append({"time": now.isoformat(timespec="seconds"), "field": "target", "old": trade.target, "new": None,
                    "note": "target removed: the trailing stop lets the trade run"})
    trade.trail, trade.history = state, log
    return events


def close(trade: PaperTrade, price: float, reason: str, ts: datetime) -> None:
    sign = 1 if trade.side == "demand" else -1
    trade.exit_price, trade.exit_ts, trade.exit_reason = float(price), ts, reason
    trade.pnl = round((price - trade.fill_price) * sign * trade.quantity, 2)
    trade.result = "win" if trade.pnl > 0 else "loss"
    trade.result_r = round((price - trade.fill_price) * sign / trade.risk, 3) if trade.risk else None


def _event(kind: str, t, price: float, ts: datetime) -> dict:
    return {"type": "paper", "event": kind, "instrument_key": t.instrument_key, "timeframe": t.timeframe, "side": t.side,
            "price": price, "quantity": t.quantity, "pnl": getattr(t, "pnl", None), "time": ts.isoformat(timespec="seconds")}


def _refresh_order(db: Session, order: PaperOrder) -> bool:
    """Keep a pending order in step with its zone and plan. Returns False if it was cancelled."""
    first = db.get(TradePlan, order.plan_id)
    z, p = db.get(Zone, order.zone_id), current_plan(db, order.zone_id, first.mode if first else None)
    if first is not None and first.status == "triggered":
        return True                    # its own plan was entered: the order is about to be filled, whatever came after
    if not z or z.status not in ACTIVE or not p or p.status == "cancelled":
        order.status, order.note = "cancelled", f"zone {z.status if z else 'removed'}"
        return False
    if p.status == "pending" and (p.entry, p.stop, p.target, p.entry_type or "TYPE_1") != (order.entry, order.stop, order.target, order.entry_type or "TYPE_1"):
        order.plan_id, order.entry, order.stop, order.target = p.id, p.entry, p.stop, p.target   # the plan got a new version
        order.entry_type = p.entry_type or "TYPE_1"
        order.quantity = quantity(db, order.instrument_key, p.entry, p.stop)
    return True


def _confirmed_fill(db: Session, order: PaperOrder) -> list[dict]:
    """Entry types 2 and 3 are not limit orders: they fill when the scan has recorded the entry on the order's plan,
    at the price and candle time of that entry. Type 2 enters at a candle's open, which is only known once that candle
    has closed, so its fill is booked one candle late and the candles since then are checked for the stop and target."""
    p = db.get(TradePlan, order.plan_id)
    if p is None or p.status != "triggered":
        return []
    ts = p.triggered_ts or now_ist()
    order.entry, order.stop, order.target = p.entry, p.stop, p.target
    order.quantity = quantity(db, order.instrument_key, p.entry, p.stop)
    if order.quantity <= 0 or p.entry == p.stop:
        order.status, order.note = "cancelled", "the entry was on the stop loss line"
        return []
    trade = _fill(db, order, ts)
    trade.checked_ts = ts - timedelta(seconds=1)      # the candles from the entry onwards are still to be checked
    return [_event("filled", trade, order.entry, ts)]


def is_limit(order: PaperOrder) -> bool:
    return (order.entry_type or "TYPE_1") == "TYPE_1"


def on_prices(db: Session, prices: dict[str, float], now: datetime) -> list[dict]:
    """Apply one round of live prices: fill pending orders, then check stops and targets."""
    events = []
    for order in live_orders(db):
        if not _refresh_order(db, order):
            continue
        if not is_limit(order):
            events.extend(_confirmed_fill(db, order))
            continue
        ltp = prices.get(order.instrument_key)
        if ltp is not None and fills.entry_touched(order.side, order.entry, ltp, ltp):
            if fills.stop_hit(order.side, order.stop, ltp, ltp):
                # the price jumped past the entry and the stop between two checks: no fill, the order is cancelled
                order.status, order.note = "cancelled", f"price jumped past the stop ({ltp:g}): not filled"
                events.append(_event("cancelled", order, ltp, now))
                continue
            price = fills.limit_fill_price(order.side, order.entry, ltp)   # the real price: the entry, or beyond it
            trade = _fill(db, order, now, price)
            events.append(_event("filled", trade, price, now))
    for trade in open_trades(db):
        ltp = prices.get(trade.instrument_key)
        if ltp is None or trade.kind == "option":      # option positions are closed by hand
            continue
        if fills.stop_hit(trade.side, trade.stop, ltp, ltp):
            close(trade, ltp, _stop_reason(trade), now)   # a stop is a market exit: you get the price that is there
            events.append(_event("stopped", trade, ltp, now))
        elif (ltp >= live_target(trade)) if trade.side == "demand" else (ltp <= live_target(trade)):
            close(trade, trade.target, "target", now)  # a target is a limit exit: you get the target price
            events.append(_event("target hit", trade, trade.target, now))
        else:
            events.extend(see_live_price(db, trade, ltp, now))
    db.commit()
    return events


def see_live_price(db: Session, trade: PaperTrade, ltp: float, now: datetime) -> list[dict]:
    """Trailing on live prices: a new best price is kept on the position and the stop moves at once (checked after
    the stop, so a price is never used to move the stop it has already hit)."""
    st = trade.trail
    if not trailing.is_on(st) or st.get("trail_tf") != "live" or trade.exit_ts is not None:
        return []
    best = st.get("live_best")
    if best is not None and ((ltp <= best) if trade.side == "demand" else (ltp >= best)):
        return []
    if best is None and ((ltp <= trade.fill_price) if trade.side == "demand" else (ltp >= trade.fill_price)):
        return []
    trade.trail = {**st, "live_best": float(ltp), "live_ts": now.isoformat(timespec="seconds")}
    return update_trailing(db, trade, now)


def on_candles(db: Session, key: str) -> list[dict]:
    """Safety net, run after each bar close: replay the stored 5-minute candles that the order or
    position has not seen yet, with the same rules as the backtest. Catches anything missed between polls."""
    events = []
    for order in live_orders(db, key):
        if not _refresh_order(db, order):
            continue
        if not is_limit(order):
            events.extend(_confirmed_fill(db, order))
            continue
        since = max(order.created_at, order.checked_ts + timedelta(minutes=5)) if order.checked_ts else order.created_at
        df = load_candles(db, key, "5m", since=since)
        for c in df.itertuples():
            ts = c.ts.to_pydatetime()
            order.checked_ts = ts
            if fills.entry_touched(order.side, order.entry, c.high, c.low):
                price = fills.limit_fill_price(order.side, order.entry, c.open)     # a gap past the entry fills at the open
                if fills.stop_hit(order.side, order.stop, price, price):           # opened beyond the stop: no fill
                    order.status, order.note = "cancelled", f"price opened past the stop ({price:g}): not filled"
                    events.append(_event("cancelled", order, price, ts))
                    break
                trade = _fill(db, order, ts, price)
                trade.checked_ts = ts
                events.append(_event("filled", trade, price, ts))
                if fills.stop_hit(order.side, order.stop, c.high, c.low):       # entry and stop in one candle: a loss
                    close(trade, order.stop, "stop", ts)
                    events.append(_event("stopped", trade, order.stop, ts))
                break
    for trade in open_trades(db, key):
        if trade.kind == "option":
            continue
        after = trade.checked_ts or trade.fill_ts
        df = load_candles(db, key, "5m", since=after + timedelta(seconds=1))
        for c in df.itertuples():
            ts = c.ts.to_pydatetime()
            trade.checked_ts = ts
            hit = fills.exit_after_entry(trade.side, trade.stop, live_target(trade), c.open, c.high, c.low)
            if hit:
                close(trade, hit[0], _stop_reason(trade) if hit[1] == "loss" else "target", ts)
                events.append(_event("stopped" if hit[1] == "loss" else "target hit", trade, hit[0], ts))
                break
    for trade in open_trades(db, key):     # after the candles are checked: a trailing stop moves at the candle close
        events.extend(update_trailing(db, trade, now_ist()))
    db.commit()
    return events


def enter_now(db: Session, order_id: int, price: float, now: datetime) -> PaperTrade:
    """Fill a pending order at once at the market price (plus the slippage setting), whatever its entry type.
    The stop loss stays on the zone's distal line; the target is moved so the trade keeps its reward-to-risk
    (target_rr times the new risk), and the quantity is sized again for the new risk."""
    order = db.get(PaperOrder, order_id)
    if not order or order.status != "pending" or order.archived:
        raise ValueError("No pending order with that id.")
    slip = float(settings_store.get(db, "paper_slippage_pct")) / 100.0
    demand = order.side == "demand"
    fill = round(price * (1 + slip) if demand else price * (1 - slip), 2)
    if (fill <= order.stop) if demand else (fill >= order.stop):
        raise ValueError(f"The market price {fill} is already beyond the stop loss {order.stop}, so the order cannot be entered.")
    rr = float(settings_store.get(db, "target_rr"))
    target = round(fill + (1 if demand else -1) * rr * abs(fill - order.stop), 2)
    qty = quantity(db, order.instrument_key, fill, order.stop)
    if qty <= 0:
        raise ValueError("The position size works out at zero for this risk.")
    order.entry, order.target, order.quantity = fill, target, qty
    order.note = f"entered at market by you ({order.entry_type or 'TYPE_1'} order)"
    trade = _fill(db, order, now)
    trade.checked_ts = now
    db.commit()
    return trade


def manual_close(db: Session, trade_id: int, price: float, now: datetime) -> PaperTrade:
    trade = db.get(PaperTrade, trade_id)
    if not trade or trade.exit_ts is not None:
        raise ValueError("No open position with that id.")
    slip = float(settings_store.get(db, "paper_slippage_pct")) / 100.0
    close(trade, price * (1 - slip) if trade.side == "demand" else price * (1 + slip), "manual", now)
    db.commit()
    return trade


def edit(db: Session, trade_id: int, stop: float | None, target: float | None, now: datetime) -> PaperTrade:
    trade = db.get(PaperTrade, trade_id)
    if not trade or trade.exit_ts is not None or trade.kind == "option":
        raise ValueError("No open zone position with that id.")
    new_stop = trade.stop if stop is None else stop
    new_target = trade.target if target is None else target
    if (new_stop >= new_target) if trade.side == "demand" else (new_stop <= new_target):
        raise ValueError("The stop and target are the wrong way round for this position.")
    log = list(trade.history or [])
    for field, value in (("stop", new_stop), ("target", new_target)):
        if value != getattr(trade, field):
            log.append({"time": now.isoformat(timespec="seconds"), "field": field, "old": getattr(trade, field), "new": value})
            setattr(trade, field, value)
    trade.history = log
    db.commit()
    return trade


def open_option(db: Session, option_key: str, label: str, price: float, lots: int, now: datetime) -> PaperTrade:
    """A bought option held until you close it. No stop or target is simulated."""
    inst = db.get(Instrument, option_key)
    qty = (inst.lot_size if inst and inst.lot_size else 1) * max(1, lots)
    trade = PaperTrade(order_id=0, instrument_key=option_key, side="demand", quantity=qty, fill_price=price, fill_ts=now,
                       stop=0.0, target=0.0, kind="option", label=label, archived=False, history=[])
    db.add(trade)
    db.commit()
    return trade


def auto_check(db: Session, rows: list, now: datetime) -> tuple[list[tuple], list[dict]]:
    """Which watched setups automatic paper trading takes now, and why the others are skipped. rows: (zone, plan)."""
    floor = float(settings_store.get(db, "paper_auto_min_score"))
    take, skip = [], []
    for z, p in rows:
        name = z.instrument_key
        if z.score < floor:
            skip.append({"zone_id": z.id, "instrument_key": name, "why": f"score {z.score:g} is below your auto score {floor:g}"})
            continue
        taken = db.scalars(select(PaperOrder).where(PaperOrder.zone_id == z.id, PaperOrder.archived.isnot(True),
                                                    PaperOrder.status.in_(("pending", "filled")))).first()
        if taken:
            skip.append({"zone_id": z.id, "instrument_key": name, "why": "an order already exists for this zone"})
            continue
        take.append((z, p))
    return take, skip


def auto_run(db: Session, rows: list, now: datetime, note: str | None = None) -> dict:
    """Place an automatic paper order for every setup auto_check takes. note: written on each new order (for example
    that it was placed while the market was closed). Returns the orders placed and the setups skipped, with reasons."""
    take, skip = auto_check(db, rows, now)
    placed = []
    for z, p in take:
        try:
            o = place(db, z.id, now, p.mode)
        except ValueError as exc:
            skip.append({"zone_id": z.id, "instrument_key": z.instrument_key, "why": str(exc)})
            continue
        if note:
            o.note = note[:255]
            db.commit()
        placed.append({"order_id": o.id, "zone_id": z.id, "instrument_key": z.instrument_key, "side": o.side, "entry": o.entry,
                       "stop": o.stop, "target": o.target, "quantity": o.quantity, "entry_type": o.entry_type})
    return {"placed": placed, "skipped": skip}


def auto_orders(db: Session, rows: list, now: datetime) -> int:
    """Auto mode in market hours (the live price loop): place a paper order for every watched setup at or above the chosen score."""
    if not settings_store.get(db, "paper_auto"):
        return 0
    return len(auto_run(db, rows, now)["placed"])


def summary(db: Session, prices: dict[str, float]) -> dict:
    capital = float(settings_store.get(db, "paper_capital"))
    closed = db.scalars(select(PaperTrade).where(PaperTrade.exit_ts.isnot(None), PaperTrade.archived.isnot(True))).all()
    opened = open_trades(db)
    realised = sum(t.pnl or 0 for t in closed)
    open_pnl = 0.0
    for t in opened:
        ltp = prices.get(t.instrument_key)
        if ltp is not None:
            open_pnl += (ltp - t.fill_price) * (1 if t.side == "demand" else -1) * t.quantity
    rs = [t.result_r for t in closed if t.result_r is not None]
    wins = sum(1 for t in closed if t.result == "win")
    return {"capital": capital, "balance": round(capital + realised, 2), "realised": round(realised, 2), "open_pnl": round(open_pnl, 2),
            "closed_trades": len(closed), "open_positions": len(opened), "pending_orders": len(live_orders(db)),
            "win_rate": round(100 * wins / len(closed), 1) if closed else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
            "auto": bool(settings_store.get(db, "paper_auto")), "auto_min_score": settings_store.get(db, "paper_auto_min_score"),
            "auto_closed": bool(settings_store.get(db, "paper_auto_closed")),
            "risk_pct": settings_store.get(db, "paper_risk_pct")}


def reset(db: Session) -> int:
    n = 0
    for model in (PaperOrder, PaperTrade):
        for row in db.scalars(select(model).where(model.archived.isnot(True))).all():
            row.archived = True
            n += 1
    db.commit()
    return n
