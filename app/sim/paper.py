"""Paper trading. No order is ever sent to Upstox: fills are simulated against live prices,
with stored 5-minute candles as a safety net for anything the price polling missed.
The fill rules are the ones the backtest uses (app/sim/fills.py)."""
import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import settings_store
from ..data.sync import load_candles, now_ist
from ..engine.link import ACTIVE
from ..engine.plan import current_plan
from ..models import Instrument, PaperOrder, PaperTrade, TradePlan, Zone
from . import fills


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


def _fill(db: Session, order: PaperOrder, ts: datetime) -> PaperTrade:
    order.status = "filled"
    trade = PaperTrade(order_id=order.id, zone_id=order.zone_id, instrument_key=order.instrument_key, timeframe=order.timeframe,
                       side=order.side, quantity=order.quantity, fill_price=order.entry, fill_ts=ts, stop=order.stop,
                       target=order.target, risk=abs(order.entry - order.stop), kind="zone", archived=False, history=[])
    db.add(trade)
    db.flush()
    return trade


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
            trade = _fill(db, order, now)
            events.append(_event("filled", trade, order.entry, now))
    for trade in open_trades(db):
        ltp = prices.get(trade.instrument_key)
        if ltp is None or trade.kind == "option":      # option positions are closed by hand
            continue
        if fills.stop_hit(trade.side, trade.stop, ltp, ltp):
            close(trade, ltp, "stop", now)             # a stop is a market exit: you get the price that is there
            events.append(_event("stopped", trade, ltp, now))
        elif (ltp >= trade.target) if trade.side == "demand" else (ltp <= trade.target):
            close(trade, trade.target, "target", now)  # a target is a limit exit: you get the target price
            events.append(_event("target hit", trade, trade.target, now))
    db.commit()
    return events


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
                trade = _fill(db, order, ts)
                trade.checked_ts = ts
                events.append(_event("filled", trade, order.entry, ts))
                if fills.stop_hit(order.side, order.stop, c.high, c.low):   # entry and stop in one candle: a loss
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
            hit = fills.exit_after_entry(trade.side, trade.stop, trade.target, c.open, c.high, c.low)
            if hit:
                close(trade, hit[0], "stop" if hit[1] == "loss" else "target", ts)
                events.append(_event("stopped" if hit[1] == "loss" else "target hit", trade, hit[0], ts))
                break
    db.commit()
    return events


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


def auto_orders(db: Session, rows: list, now: datetime) -> int:
    """Auto mode: place a paper order for every watched setup at or above the chosen score."""
    if not settings_store.get(db, "paper_auto"):
        return 0
    floor, placed = int(settings_store.get(db, "paper_auto_min_score")), 0
    for z, _ in rows:
        if z.score >= floor:
            try:
                place(db, z.id, now)
                placed += 1
            except ValueError:
                pass   # already has an order
    return placed


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
            "risk_pct": settings_store.get(db, "paper_risk_pct")}


def reset(db: Session) -> int:
    n = 0
    for model in (PaperOrder, PaperTrade):
        for row in db.scalars(select(model).where(model.archived.isnot(True))).all():
            row.archived = True
            n += 1
    db.commit()
    return n
