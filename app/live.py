"""Live mode: polls last traded prices for instruments that have setups, raises alerts,
and pushes prices and alerts to the pages over a WebSocket."""
import asyncio
from datetime import datetime, time, timedelta

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .config import SESSION_CLOSE, SESSION_OPEN
from .data.sync import now_ist
from .db import SessionLocal
from .alerts import telegram
from .models import Instrument, TradePlan, Zone, ZoneEvent
from .sim import paper
from .upstox.client import UpstoxError

POLL_SECONDS = 5
STALE_AFTER = 30   # seconds without a successful poll before prices are shown as stale


class Hub:
    """Pages connected to /ws/live."""
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def broadcast(self, message: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                self.clients.discard(ws)


hub = Hub()
state = {"prices": {}, "last_poll": None, "error": None, "running": False, "last_scan": None, "task": None}
_sent: set[tuple[int, str]] = set()   # (zone id, alert type) already raised


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_OPEN <= now.time() <= SESSION_CLOSE


def next_bar_close(now: datetime) -> datetime | None:
    """Next 5-minute bar close in today's session, or None outside it."""
    if now.weekday() >= 5 or now.time() >= SESSION_CLOSE:
        return None
    first = datetime.combine(now.date(), time(9, 20))
    if now < first:
        return first
    mins = (now - datetime.combine(now.date(), SESSION_OPEN)).seconds // 60
    return datetime.combine(now.date(), SESSION_OPEN) + timedelta(minutes=(mins // 5 + 1) * 5)


def watch_rows(db: Session) -> list:
    """Fresh zones with a pending plan in some mode (the HTF curve and ITF trend allow the trade) at or above the minimum score."""
    floor = settings_store.get(db, "min_score")
    return db.execute(select(Zone, TradePlan).join(TradePlan, TradePlan.zone_id == Zone.id).where(
        Zone.status == "fresh", TradePlan.is_current.is_(True), TradePlan.status == "pending",
        TradePlan.allowed.is_(True), Zone.score >= floor)).all()


def check_prices(db: Session, prices: dict[str, float], now: datetime) -> list[dict]:
    """Compare live prices with every watched zone. Each alert fires once per zone:
    approaching (within approach_pct percent of the proximal line) and triggered (price reaches the proximal line).
    Reaching the line fills an entry type 1 plan. A plan waiting for entry type 2 or 3 stays pending until the scan sees the candles confirm."""
    rows = watch_rows(db)
    if not rows:
        return []
    ids = [z.id for z, _ in rows]
    for zid, etype in db.execute(select(ZoneEvent.zone_id, ZoneEvent.event_type).where(
            ZoneEvent.zone_id.in_(ids), ZoneEvent.event_type.in_(("alert_approaching", "alert_triggered")))):
        _sent.add((zid, etype))
    reach = float(settings_store.get(db, "approach_pct")) / 100.0
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(
        Instrument.instrument_key.in_({z.instrument_key for z, _ in rows}))).all())
    alerts = []
    for z, p in rows:
        ltp = prices.get(z.instrument_key)
        if ltp is None:
            continue
        if z.side == "demand":
            reached, near = ltp <= z.proximal, z.proximal < ltp <= z.proximal * (1 + reach)
        else:
            reached, near = ltp >= z.proximal, z.proximal * (1 - reach) <= ltp < z.proximal
        kind = "alert_triggered" if reached else "alert_approaching" if near else None
        if kind is None or (z.id, kind) in _sent:
            continue
        _sent.add((z.id, kind))
        db.add(ZoneEvent(zone_id=z.id, ts=now, event_type=kind, price=ltp))
        if kind == "alert_triggered":
            if (p.entry_type or "TYPE_1") == "TYPE_1":      # a limit at the proximal line is filled; types 2 and 3 wait for the candles
                p.status, p.triggered_ts = "triggered", now
            _sent.add((z.id, "alert_approaching"))   # no approach alert once price is at the zone
        alerts.append({"type": "alert", "alert": kind[6:], "zone_id": z.id, "instrument_key": z.instrument_key,
                       "symbol": names.get(z.instrument_key) or z.instrument_key.split("|")[-1], "timeframe": z.timeframe,
                       "side": z.side, "price": ltp, "entry": p.entry, "stop": p.stop, "target": p.target,
                       "reward_risk": p.reward_risk, "score": z.score, "label": z.label, "mode": p.mode, "entry_type": p.entry_type or "TYPE_1",
                       "curve": (p.context or {}).get("curve"), "itf_trend": p.trend, "time": now.isoformat(timespec="seconds")})
    db.commit()
    return alerts


async def poll_once(client, now: datetime | None = None) -> list[dict]:
    """One price poll: the stock list, every watched instrument, and anything held on paper.
    Raises alerts, applies paper fills, and pushes all of it to the pages and to Telegram."""
    now = now or now_ist()
    with SessionLocal() as db:
        rows = watch_rows(db)
        held = {o.instrument_key for o in paper.live_orders(db)} | {t.instrument_key for t in paper.open_trades(db)}
        keys = sorted(set(settings_store.get(db, "universe") or []) | {z.instrument_key for z, _ in rows} | held)
        if not keys:
            return []
        prices = await client.ltp(keys)
        state["prices"].update(prices)
        state["last_poll"], state["error"] = now, None
        placed = paper.auto_orders(db, rows, now)        # before the alert check, while the plans are still pending
        fills_ = paper.on_prices(db, prices, now)
        alerts = check_prices(db, prices, now)
    await hub.broadcast({"type": "prices", "data": prices, "time": now.isoformat(timespec="seconds")})
    for a in alerts:
        await hub.broadcast(a)
        telegram.notify(telegram.alert_text(a))
    for e in fills_:
        await hub.broadcast(e)
        telegram.notify(telegram.paper_text(e))
    if placed and not fills_:
        await hub.broadcast({"type": "paper", "event": "order placed"})
    return alerts


async def poller(client) -> None:
    """Runs for the life of the app; polls only while the market is open."""
    state["running"] = True
    try:
        while True:
            if market_open(now_ist()):
                try:
                    await poll_once(client)
                except UpstoxError as exc:      # token missing or expired, or an Upstox error: shown on the page
                    state["error"] = str(exc)
                except Exception as exc:
                    state["error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(POLL_SECONDS)
            else:
                await asyncio.sleep(20)
    finally:
        state["running"] = False


def start_poller(client) -> None:
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    state["task"] = asyncio.get_running_loop().create_task(poller(client))


def feed_status(now: datetime) -> dict:
    last = state["last_poll"]
    return {"running": state["running"], "last_poll": last, "error": state["error"],
            "stale": bool(market_open(now) and (last is None or (now - last).total_seconds() > STALE_AFTER))}
