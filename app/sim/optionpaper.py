"""Option paper trading: the scanner's signal traded with a bought option, in its own paper account.

A BUY signal buys a call (CE), a SELL signal buys a put (PE); options are never sold here. The strike comes from the
option chain by the strike rule of the settings, the number of lots from the risk: the estimated premium at the stock's
entry minus the estimated premium at the stock's stop, per lot. The order waits for the STOCK to reach the entry; the
stop, the target and the trailing stop all follow the stock's price, and the option is sold at its live price when one
of them is hit, on the extra premium stop (if set), or on the expiry day at the closing time of the settings.
Paper only: no order is ever sent to a broker."""
from __future__ import annotations

import math
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import settings_store
from ..engine.confirm import premium_at
from ..models import Instrument, OptionPaperTrade, TradePlan, Zone
from ..options import chain as options
from . import trailing

RULES = ("delta", "atm", "otm1", "target")
RULE_TEXT = {"delta": "delta nearest 0.55", "atm": "at the money", "otm1": "one strike out of the money", "target": "strike nearest the target"}
TARGET_DELTA = 0.55
OPEN_STATES = ("pending", "open")


def _s(db: Session, key: str):
    return settings_store.get(db, key)


# ---------------------------------------------------------------- choosing the option
def pick_expiry(expiries: list[str], today: date, min_days: int) -> str | None:
    """The nearest expiry with at least min_days left; the last one if none has that many."""
    for e in sorted(expiries):
        if (date.fromisoformat(e) - today).days >= min_days:
            return e
    return None


def pick_strike(strikes: list[dict], signal: str, rule: str, spot: float, target: float) -> tuple[dict | None, dict | None, str]:
    """The strike row and the leg (call on BUY, put on SELL) by the rule. Returns (row, leg, why) with row None when
    nothing fits; why says what was chosen or why not."""
    leg_name = "call" if signal == "BUY" else "put"
    rows = sorted((s for s in strikes if s.get(leg_name)), key=lambda s: s["strike"])
    if not rows:
        return None, None, "the option chain has no contracts on this side"
    atm = min(rows, key=lambda s: abs(s["strike"] - spot))
    if rule == "delta":
        cands = [s for s in rows if s[leg_name].get("delta") is not None]
        liquid = [s for s in cands if s[leg_name].get("liquid")]
        if not cands:
            return None, None, "the option chain has no delta to choose by"
        row = min(liquid or cands, key=lambda s: abs(abs(s[leg_name]["delta"]) - TARGET_DELTA))
    elif rule == "atm":
        row = atm
    elif rule == "otm1":
        i = rows.index(atm)
        j = i + 1 if signal == "BUY" else i - 1                  # a call is out of the money above, a put below
        if not 0 <= j < len(rows):
            return None, None, "no strike beyond the money in the chain"
        row = rows[j]
    else:
        row = min(rows, key=lambda s: abs(s["strike"] - target))
    leg = row[leg_name]
    if not leg.get("liquid"):
        return None, None, f"{row['strike']:g} {'CE' if signal == 'BUY' else 'PE'} is not liquid (spread or open interest)"
    if not leg.get("ltp"):
        return None, None, f"{row['strike']:g} {'CE' if signal == 'BUY' else 'PE'} has no price"
    return row, leg, RULE_TEXT.get(rule, rule)


def size(leg: dict, spot: float, entry: float, stop: float, target: float, lot: int, budget: float,
         max_premium: float) -> dict:
    """Estimated premiums at the stock's entry, stop and target, and the lots: as many as the risk budget allows,
    then no more than the premium cap. lots 0 comes with the reason."""
    at = {k: premium_at(leg, spot, v) for k, v in (("entry", entry), ("stop", stop), ("target", target))}
    out = {"est_entry": at["entry"], "est_stop": at["stop"], "est_target": at["target"], "lots": 0, "why": None,
           "risk_per_lot": None}
    if None in at.values():
        out["why"] = "no delta to estimate the premium"
        return out
    risk_unit = at["entry"] - at["stop"]
    if risk_unit <= 0:
        out["why"] = "the option would not lose at the stop (check the chain)"
        return out
    per_lot = risk_unit * lot
    out["risk_per_lot"] = round(per_lot, 2)
    lots = int(budget // per_lot)
    if lots <= 0:
        out["why"] = f"1 lot risks about ₹{per_lot:,.0f}, above your ₹{budget:,.0f} per trade"
        return out
    cap = int(max_premium // (at["entry"] * lot)) if at["entry"] > 0 else lots
    if cap <= 0:
        out["why"] = f"1 lot costs about ₹{at['entry'] * lot:,.0f}, above the premium limit of ₹{max_premium:,.0f}"
        return out
    out["lots"] = min(lots, cap)
    out["capped"] = cap < lots
    return out


# ---------------------------------------------------------------- the account
def balance(db: Session) -> float:
    closed = db.scalars(select(OptionPaperTrade).where(OptionPaperTrade.status == "closed", OptionPaperTrade.archived.isnot(True))).all()
    return float(_s(db, "opt_paper_capital")) + sum((t.pnl or 0) - (t.charges or 0) for t in closed)


def in_positions(db: Session) -> float:
    rows = db.scalars(select(OptionPaperTrade).where(OptionPaperTrade.status == "open", OptionPaperTrade.archived.isnot(True))).all()
    return sum((t.fill_premium or 0) * (t.quantity or 0) for t in rows)


def charges(buy_value: float, sell_value: float, th: dict) -> float:
    """NSE options, bought and sold: brokerage per order (or its % if lower), STT on the sell premium, exchange charge
    on the premium both sides, SEBI fee, GST, stamp duty on the buy."""
    pct = lambda key: float(th.get(key) or 0) / 100.0

    def brokerage(value: float) -> float:
        flat, share = float(th.get("brokerage_per_order") or 0), pct("brokerage_pct") * value
        return min(flat, share) if flat > 0 and share > 0 else max(flat, share)
    turnover = buy_value + sell_value
    brok = brokerage(buy_value) + brokerage(sell_value)
    stt = pct("opt_stt_pct") * sell_value
    exchange = pct("opt_exchange_pct") * turnover
    sebi = float(th.get("sebi_fee_per_crore") or 0) * turnover / 1e7
    gst = pct("gst_pct") * (brok + exchange + sebi)
    stamp = pct("stamp_intraday_pct") * buy_value
    return round(brok + stt + exchange + sebi + gst + stamp, 2)


def _th(db: Session) -> dict:
    keys = ("brokerage_per_order", "brokerage_pct", "opt_stt_pct", "opt_exchange_pct", "sebi_fee_per_crore", "gst_pct", "stamp_intraday_pct")
    return {k: _s(db, k) for k in keys}


# ---------------------------------------------------------------- placing
def existing(db: Session, plan: TradePlan) -> OptionPaperTrade | None:
    return db.scalars(select(OptionPaperTrade).where(OptionPaperTrade.zone_id == plan.zone_id, OptionPaperTrade.mode == plan.mode,
                                                     OptionPaperTrade.status.in_(OPEN_STATES), OptionPaperTrade.archived.isnot(True))).first()


async def choose(db: Session, client, plan: TradePlan, now: datetime, spot: float | None = None) -> dict:
    """The option for one plan, from the live option chain: expiry, strike, premium now, estimated premiums at the
    stock's entry, stop and target, lots and cost. {"skipped": why} when it cannot be traded."""
    zone = db.get(Zone, plan.zone_id)
    if zone is None:
        return {"skipped": "the zone of this plan no longer exists"}
    key = zone.instrument_key
    signal = "BUY" if zone.side == "demand" else "SELL"
    exps = options.expiries(db, key, now.date())
    if not exps:
        return {"skipped": "no option contracts for this stock (refresh instruments, or it has no options)"}
    min_days = int(_s(db, "opt_min_days") or 0)
    expiry = pick_expiry(exps, now.date(), min_days)
    if expiry is None:
        return {"skipped": f"no expiry with at least {min_days} days left"}
    view = options.build_view(db, await client.option_chain(key, expiry), zone)
    spot = view.get("spot") or spot
    if not spot:
        return {"skipped": "no price for the stock"}
    rule = _s(db, "opt_strike_rule") if _s(db, "opt_strike_rule") in RULES else "delta"
    row, leg, why = pick_strike(view["strikes"], signal, rule, spot, plan.target)
    if row is None:
        return {"skipped": why}
    lot = int(db.scalar(select(Instrument.lot_size).where(Instrument.instrument_key == leg["instrument_key"])) or 1)
    bal = balance(db)
    budget = bal * float(_s(db, "opt_paper_risk_pct")) / 100
    sized = size(leg, spot, plan.entry, plan.stop, plan.target, lot, budget, bal * float(_s(db, "opt_max_premium_pct")) / 100)
    otype = "CE" if signal == "BUY" else "PE"
    label = f"{_symbol(db, key)} {date.fromisoformat(expiry):%d %b} {row['strike']:g} {otype}".upper()
    out = {"zone": zone, "key": key, "signal": signal, "expiry": expiry, "days_left": (date.fromisoformat(expiry) - now.date()).days,
           "rule": rule, "why": why, "strike": row["strike"], "option_type": otype, "label": label, "leg": leg, "spot": spot,
           "premium": leg.get("ltp"), "lot": lot, "lots": sized["lots"], "capped": sized.get("capped", False),
           "est_entry": sized["est_entry"], "est_stop": sized["est_stop"], "est_target": sized["est_target"],
           "risk_per_lot": sized["risk_per_lot"]}
    if sized["lots"] <= 0:
        out["skipped"] = f"{label}: {sized['why']}"
    return out


async def place(db: Session, client, plan: TradePlan, now: datetime, spot: float | None = None, note: str | None = None,
                placed_by: str = "you") -> dict:
    """Choose the option for one plan and save it as a pending order. Returns {"placed": row} or {"skipped": why}."""
    if existing(db, plan):
        return {"skipped": "an option order already exists for this zone"}
    c = await choose(db, client, plan, now, spot)
    if "skipped" in c:
        return {"skipped": c["skipped"]}
    zone, leg, lots = c["zone"], c["leg"], c["lots"]
    t = OptionPaperTrade(status="pending", underlying_key=c["key"], symbol=_symbol(db, c["key"]), mode=plan.mode, timeframe=zone.timeframe,
                         zone_id=zone.id, plan_id=plan.id, signal=c["signal"], score=zone.score, entry_type=plan.entry_type or "TYPE_1",
                         signal_ts=plan.created_ts, option_key=leg["instrument_key"], option_label=c["label"], option_type=c["option_type"],
                         strike=c["strike"], expiry=c["expiry"], strike_rule=c["rule"], delta=leg.get("delta"), gamma=leg.get("gamma"),
                         placed_by=placed_by, lot_size=c["lot"], lots=lots, quantity=lots * c["lot"], entry=plan.entry, stop=plan.stop,
                         target=plan.target, first_stop=plan.stop, first_target=plan.target, est_entry=c["est_entry"], est_stop=c["est_stop"],
                         est_target=c["est_target"], risk_amount=round(c["risk_per_lot"] * lots, 2), created_at=now,
                         last_premium=leg.get("ltp"), last_spot=c["spot"],
                         note=note or (f"{c['why']}; lots limited by the premium cap" if c["capped"] else c["why"]), history=[], archived=False)
    db.add(t)
    db.commit()
    return {"placed": t}


def option_stocks(db: Session, keys, today: date) -> set[str]:
    """The stocks among `keys` that have option contracts not yet expired (from the instrument file)."""
    keys = set(keys)
    if not keys:
        return set()
    rows = db.execute(select(Instrument.underlying_key).where(
        Instrument.underlying_key.in_(keys), Instrument.segment == "NSE_FO", Instrument.instrument_type.in_(("CE", "PE")),
        Instrument.expiry >= datetime.combine(today, datetime.min.time())).distinct()).all()
    return {r[0] for r in rows}


# ---------------------------------------------------------------- the option for each signal, before placing
_previews: dict[int, tuple[datetime, dict]] = {}     # plan id -> (when, preview); kept 15 minutes
PREVIEW_MINUTES = 15


def _preview_of(c: dict) -> dict:
    keep = ("label", "strike", "option_type", "expiry", "days_left", "premium", "est_entry", "est_stop", "est_target", "lots", "lot",
            "risk_per_lot", "skipped")
    out = {k: c.get(k) for k in keep}
    out["option_key"] = (c.get("leg") or {}).get("instrument_key")
    if c.get("lots") and c.get("est_entry"):
        out["cost"] = round(c["est_entry"] * c["lot"] * c["lots"], 2)
        out["risk"] = round((c.get("risk_per_lot") or 0) * c["lots"], 2)
    return out


async def previews(db: Session, client, plans: list[TradePlan], now: datetime, prices: dict | None = None) -> dict[int, dict]:
    """The option each signal would get (strike, premium, lots), from the option chain at most every 15 minutes."""
    out, todo = {}, []
    for p in plans:
        got = _previews.get(p.id)
        if got and (now - got[0]).total_seconds() < PREVIEW_MINUTES * 60:
            out[p.id] = got[1]
        else:
            todo.append(p)
    for p in todo:                                         # one at a time: the session is not shared across tasks
        try:
            zone = db.get(Zone, p.zone_id)
            c = await choose(db, client, p, now, (prices or {}).get(zone.instrument_key) if zone else None)
        except Exception as exc:                           # one stock's chain failing does not stop the others
            c = {"skipped": f"option chain: {type(exc).__name__}"}
        v = _preview_of(c)
        _previews[p.id] = (now, v)
        out[p.id] = v
    return out


def _symbol(db: Session, key: str) -> str:
    return db.scalar(select(Instrument.symbol).where(Instrument.instrument_key == key)) or key.split("|")[-1]


# ---------------------------------------------------------------- live prices
def live(db: Session) -> list[OptionPaperTrade]:
    return db.scalars(select(OptionPaperTrade).where(OptionPaperTrade.status.in_(OPEN_STATES), OptionPaperTrade.archived.isnot(True))).all()


def preview_keys(now: datetime) -> set[str]:
    """The options shown for the signals in the last 15 minutes: their premium updates live on the page too."""
    return {v["option_key"] for when, v in _previews.values()
            if v.get("option_key") and (now - when).total_seconds() < PREVIEW_MINUTES * 60}


def watch_keys(db: Session, now: datetime | None = None) -> set[str]:
    """The stocks and the options the price round must fetch: open and pending option trades, and the options
    shown for the signals."""
    out = preview_keys(now) if now is not None else set()
    for t in live(db):
        out.add(t.underlying_key)
        if t.option_key:
            out.add(t.option_key)
    return out


def _event(kind: str, t: OptionPaperTrade, now: datetime, **extra) -> dict:
    return {"type": "option-paper", "event": kind, "id": t.id, "label": t.option_label, "symbol": t.symbol,
            "time": now.isoformat(timespec="seconds"), **extra}


def _beyond(signal: str, price: float, level: float) -> bool:
    """The stock at or past a level against the trade (the stop)."""
    return price <= level if signal == "BUY" else price >= level


def _reached(signal: str, price: float, level: float) -> bool:
    """The stock at or past a level in the trade's favour (the target)."""
    return price >= level if signal == "BUY" else price <= level


def _trail_start(db: Session, t: OptionPaperTrade) -> dict | None:
    opts = trailing.options({k: _s(db, k) for k in trailing.OPTION_KEYS})
    if not trailing.is_on(opts) or opts["trail_method"] not in ("lock", "step"):
        return None                                   # swing and ATR need candles of the stock: not used for options
    return {**opts, "first_stop": t.first_stop, "first_target": t.first_target, "moves": 0,
            "no_target": trailing.no_target_from_fill(opts)}


def fill(db: Session, t: OptionPaperTrade, premium: float, spot: float, now: datetime) -> None:
    t.status, t.fill_ts, t.fill_premium, t.fill_spot = "open", now, float(premium), float(spot)
    t.best_spot, t.last_premium, t.last_spot = float(spot), float(premium), float(spot)
    t.trail = _trail_start(db, t)
    t.history = list(t.history or []) + [{"time": now.isoformat(timespec="seconds"), "field": "fill", "old": None, "new": premium,
                                           "note": f"bought at {premium:g} with the stock at {spot:g}"}]


def close(db: Session, t: OptionPaperTrade, premium: float, spot: float | None, reason: str, now: datetime) -> None:
    t.status, t.exit_ts, t.exit_premium, t.exit_spot, t.exit_reason = "closed", now, float(premium), spot, reason
    qty = t.quantity or 0
    t.pnl = round((float(premium) - (t.fill_premium or 0)) * qty, 2)
    t.charges = charges((t.fill_premium or 0) * qty, float(premium) * qty, _th(db))
    t.result_r = round((t.pnl - t.charges) / t.risk_amount, 3) if t.risk_amount else None


def _trail(db: Session, t: OptionPaperTrade, now: datetime) -> list[dict]:
    """Move the stop (and the lock's target) from the best stock price since the fill."""
    st = t.trail
    if not st or t.best_spot is None:
        return []
    side = "demand" if t.signal == "BUY" else "supply"
    tr = trailing.Trailer(side, t.fill_spot, st["first_stop"], st, target=st.get("first_target"))
    tr.see_price(t.best_spot)
    events, log = [], list(t.history or [])
    better = (tr.stop > t.stop + 1e-9) if t.signal == "BUY" else (tr.stop < t.stop - 1e-9)
    if better:
        log.append({"time": now.isoformat(timespec="seconds"), "field": "stop", "old": t.stop, "new": round(tr.stop, 2),
                    "note": f"trailing stop ({st['trail_method']}): {tr.why}"})
        t.stop = round(tr.stop, 2)
        t.trail = {**st, "moves": int(st.get("moves", 0)) + 1}
        events.append(_event("stop moved", t, now, stop=t.stop))
    if tr.target is not None and abs(tr.target - t.target) > 1e-9:
        log.append({"time": now.isoformat(timespec="seconds"), "field": "target", "old": t.target, "new": round(tr.target, 2),
                    "note": "lock: the target moved forward one step"})
        t.target = round(tr.target, 2)
    t.history = log
    return events


def _expiry_close_time(db: Session) -> time:
    try:
        h, m = str(_s(db, "opt_expiry_close") or "15:15").split(":")
        return time(int(h), int(m))
    except ValueError:
        return time(15, 15)


def on_prices(db: Session, prices: dict[str, float], now: datetime) -> list[dict]:
    """One live price round: fill pending orders when the stock reaches the entry, then manage the open ones."""
    events = []
    free = balance(db) - in_positions(db)
    for t in live(db):
        spot = prices.get(t.underlying_key)
        premium = prices.get(t.option_key) if t.option_key else None
        if spot is not None:
            t.last_spot = spot
        if premium is not None:
            t.last_premium = premium
        if spot is None:
            continue
        if t.status == "pending":
            if t.expiry and date.fromisoformat(t.expiry) < now.date():
                t.status, t.note = "cancelled", "the option expired before the entry"
                events.append(_event("cancelled", t, now))
                continue
            if (t.entry_type or "TYPE_1") == "TYPE_1":
                touched = spot <= t.entry if t.signal == "BUY" else spot >= t.entry
            else:                                         # types 2 and 3: the scan marks the plan triggered
                plan = db.get(TradePlan, t.plan_id) if t.plan_id else None
                touched = plan is not None and plan.status == "triggered"
            if not touched:
                continue
            if _beyond(t.signal, spot, t.stop):
                t.status, t.note = "cancelled", f"the stock jumped past the stop ({spot:g}): not bought"
                events.append(_event("cancelled", t, now))
                continue
            if premium is None:
                t.note = "the stock is at the entry: waiting for the option's price"
                continue
            cost = premium * (t.quantity or 0)
            if cost > free:
                t.note = f"waiting for money: needs ₹{cost:,.0f}, free ₹{max(free, 0):,.0f}"
                continue
            fill(db, t, premium, spot, now)
            free -= cost
            events.append(_event("filled", t, now, premium=premium, spot=spot))
            continue
        # open
        exit_premium = premium if premium is not None else t.last_premium
        if t.expiry and date.fromisoformat(t.expiry) == now.date() and now.time() >= _expiry_close_time(db):
            close(db, t, exit_premium, spot, "expiry", now)
            events.append(_event("closed", t, now, reason="expiry"))
            continue
        if _beyond(t.signal, spot, t.stop):
            reason = "trail" if (t.trail or {}).get("moves") else "stop"
            close(db, t, exit_premium, spot, reason, now)
            events.append(_event("closed", t, now, reason=reason))
            continue
        pct = float(_s(db, "opt_premium_stop_pct") or 0)
        if pct > 0 and premium is not None and premium <= (t.fill_premium or 0) * (1 - pct / 100):
            close(db, t, premium, spot, "premium stop", now)
            events.append(_event("closed", t, now, reason="premium stop"))
            continue
        if not (t.trail or {}).get("no_target") and _reached(t.signal, spot, t.target):
            close(db, t, exit_premium, spot, "target", now)
            events.append(_event("closed", t, now, reason="target"))
            continue
        if t.best_spot is None or _reached(t.signal, spot, t.best_spot + (1e-9 if t.signal == "BUY" else -1e-9)):
            t.best_spot = spot
            events.extend(_trail(db, t, now))
    db.commit()
    return events


def close_now(db: Session, trade_id: int, now: datetime, premium: float | None = None) -> OptionPaperTrade:
    t = db.get(OptionPaperTrade, trade_id)
    if t is None or t.status not in OPEN_STATES:
        raise ValueError("No pending order or open position with that id.")
    if t.status == "pending":
        t.status, t.note = "cancelled", "cancelled by you"
    else:
        price = premium if premium is not None else t.last_premium
        if price is None:
            raise ValueError("No price for the option yet: give the price to close at.")
        close(db, t, price, t.last_spot, "manual", now)
    db.commit()
    return t


def reset(db: Session) -> int:
    rows = db.scalars(select(OptionPaperTrade).where(OptionPaperTrade.archived.isnot(True))).all()
    for t in rows:
        t.archived = True
        if t.status in OPEN_STATES:
            t.status, t.note = "cancelled", "the option paper account was reset"
    db.commit()
    return len(rows)


# ---------------------------------------------------------------- automatic orders
_tried: dict[int, datetime] = {}      # plan id -> when it was last tried (a skipped plan is tried again after 30 min)
RETRY_MINUTES = 30


async def auto_place(db: Session, client, rows: list, now: datetime, prices: dict | None = None, force: bool = False,
                     note: str | None = None) -> dict:
    """Option orders for the watched plans at or above the automatic score. Each plan is tried once (again after 30
    minutes if it was skipped), so the option chain is not fetched on every price round."""
    floor = float(_s(db, "opt_auto_min_score") or 0)
    out = {"placed": [], "skipped": []}
    has_options = option_stocks(db, {z.instrument_key for z, _ in rows}, now.date())
    for z, p in rows:
        if z.instrument_key not in has_options:                # stocks without options are not option trades
            continue
        if (z.score or 0) < floor:
            if force:
                out["skipped"].append({"symbol": _symbol(db, z.instrument_key), "mode": p.mode, "why": f"score {z.score:g} is below your auto score {floor:g}"})
            continue
        last = _tried.get(p.id)
        if not force and last is not None and (now - last).total_seconds() < RETRY_MINUTES * 60:
            continue
        _tried[p.id] = now
        if existing(db, p):
            if force:
                out["skipped"].append({"symbol": _symbol(db, z.instrument_key), "mode": p.mode, "why": "an option order already exists for this zone"})
            continue
        try:
            got = await place(db, client, p, now, (prices or {}).get(z.instrument_key), note or "placed automatically", "auto")
        except Exception as exc:                       # one stock's chain failing does not stop the others
            got = {"skipped": f"{type(exc).__name__}: {exc}"}
        sym = _symbol(db, z.instrument_key)
        if "placed" in got:
            out["placed"].append({"id": got["placed"].id, "symbol": sym, "label": got["placed"].option_label})
        else:
            out["skipped"].append({"symbol": sym, "mode": p.mode, "why": got["skipped"]})
    return out


# ---------------------------------------------------------------- the page
def estimate(t: OptionPaperTrade, level: float | None) -> float | None:
    """The option's premium if the stock were at `level`, from its latest premium and the stock's latest price
    (delta and gamma saved when the option was chosen). A guide only: no time decay, no IV change."""
    if level is None or t.delta is None:
        return None
    premium, spot = t.last_premium, t.last_spot
    if not premium or not spot:
        return None
    return premium_at({"ltp": premium, "delta": t.delta, "gamma": t.gamma}, spot, level)


def view(t: OptionPaperTrade, now: datetime) -> dict:
    qty = t.quantity or 0
    open_pnl = round(((t.last_premium or 0) - (t.fill_premium or 0)) * qty, 2) if t.status == "open" and t.last_premium is not None else None
    sign = 1 if t.signal == "BUY" else -1
    away = None
    if t.status == "pending" and t.last_spot and t.entry:
        away = round(100 * sign * (t.last_spot - t.entry) / t.entry, 2)
    lock = None
    st = t.trail or {}
    if t.status == "open" and st.get("trail_method") == "lock" and st.get("first_target") is not None:
        lock = trailing.ladder("demand" if t.signal == "BUY" else "supply", t.fill_spot, st["first_stop"], st["first_target"], st, t.stop)
    out = {c.name: getattr(t, c.name) for c in t.__table__.columns}
    est = lambda level: estimate(t, level)
    out.update({"est_stop_now": est(t.stop) if t.status in OPEN_STATES else None,
                "est_target_now": est(t.target) if t.status in OPEN_STATES else None,
                "est_entry_now": est(t.entry) if t.status == "pending" else None,
                "change_pct": round(100 * ((t.last_premium if t.status == "open" else t.exit_premium) - t.fill_premium) / t.fill_premium, 1)
                if t.fill_premium and (t.last_premium if t.status == "open" else t.exit_premium) is not None and t.status in ("open", "closed") else None,
                "open_r": round(open_pnl / t.risk_amount, 2) if open_pnl is not None and t.risk_amount else None,
                "days_left": (date.fromisoformat(t.expiry) - now.date()).days if t.expiry else None})
    out.update({"open_pnl": open_pnl, "away_pct": away, "lock": lock,
                "pnl_after": round((t.pnl or 0) - (t.charges or 0), 2) if t.status == "closed" else None,
                "open_s": ((t.exit_ts or now) - t.fill_ts).total_seconds() if t.fill_ts else None,
                "waited_s": (t.fill_ts - t.created_at).total_seconds() if t.fill_ts and t.created_at else None,
                "cost": round((t.fill_premium or t.est_entry or 0) * qty, 2)})
    return out
