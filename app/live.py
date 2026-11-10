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
from .sim import optionpaper, paper
from .upstox.client import UpstoxError

POLL_SECONDS = 5
STALE_AFTER = 30   # seconds without a successful poll before prices are shown as stale


class Hub:
    """Pages connected to /ws/live. A message goes to every page at the same time, with a time limit each: a page
    that does not take it within SEND_TIMEOUT seconds (a sleeping laptop, a frozen tab) is dropped and reconnects by
    itself, so it never holds up the other pages or the next price round."""
    SEND_TIMEOUT = 2.0

    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def _send(self, ws: WebSocket, message: dict) -> None:
        try:
            await asyncio.wait_for(ws.send_json(message), self.SEND_TIMEOUT)
        except Exception:
            self.clients.discard(ws)
            try:
                await asyncio.wait_for(ws.close(), 1)
            except Exception:
                pass

    async def broadcast(self, message: dict) -> None:
        if self.clients:
            await asyncio.gather(*(self._send(ws, message) for ws in list(self.clients)))


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
    """Fresh zones with a pending plan in some mode (the HTF curve and ITF trend allow the trade) at or above the minimum score.
    Only each mode's default (best) LTF zone, unless "Alerts and automatic paper orders on every LTF zone" is on.
    Plans made before that marking existed (is_default not set yet) count as default until the next scan marks them."""
    floor = settings_store.get(db, "min_score")
    q = select(Zone, TradePlan).join(TradePlan, TradePlan.zone_id == Zone.id).where(
        Zone.status == "fresh", TradePlan.is_current.is_(True), TradePlan.status == "pending",
        TradePlan.allowed.is_(True), Zone.score >= floor)
    if not settings_store.get(db, "ltf_alerts_all_zones"):
        q = q.where(TradePlan.is_default.isnot(False))
    return db.execute(q).all()


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


def _round_keys(now: datetime) -> list[str]:
    """What the price round fetches: the stock list, every watched instrument, anything held on paper, and the
    options of option paper (and those shown for its signals)."""
    with SessionLocal() as db:
        rows = watch_rows(db)
        held = {o.instrument_key for o in paper.live_orders(db)} | {t.instrument_key for t in paper.open_trades(db)} | optionpaper.watch_keys(db, now)
        return sorted(set(settings_store.get(db, "universe") or []) | {z.instrument_key for z, _ in rows} | held)


def _round_apply(prices: dict, now: datetime) -> tuple:
    """The database work of one price round: automatic paper orders, paper fills and exits, option paper, alerts."""
    with SessionLocal() as db:
        rows = watch_rows(db)
        placed = paper.auto_orders(db, rows, now)        # before the alert check, while the plans are still pending
        fills_ = paper.on_prices(db, prices, now)
        opt_events = optionpaper.on_prices(db, prices, now)
        alerts = check_prices(db, prices, now)
        opt_auto = bool(settings_store.get(db, "opt_auto"))
    return placed, fills_, opt_events, alerts, opt_auto


async def poll_once(client, now: datetime | None = None) -> list[dict]:
    """One price poll. The database work runs in a worker thread, so the pages and the other jobs never wait for it,
    and the prices are pushed to the pages first, before the fills and alerts are worked out."""
    now = now or now_ist()
    keys = await asyncio.to_thread(_round_keys, now)
    if not keys:
        return []
    prices = await client.ltp(keys)
    state["prices"].update(prices)
    state["last_poll"], state["error"] = now, None
    await hub.broadcast({"type": "prices", "data": prices, "time": now.isoformat(timespec="seconds")})
    placed, fills_, opt_events, alerts, opt_auto = await asyncio.to_thread(_round_apply, prices, now)
    for a in alerts:
        await hub.broadcast(a)
        telegram.notify(telegram.alert_text(a))
    for e in fills_:
        await hub.broadcast(e)
        telegram.notify(telegram.paper_text(e))
    if placed and not fills_:
        await hub.broadcast({"type": "paper", "event": "order placed"})
    for e in opt_events:
        await hub.broadcast(e)
        telegram.notify(f"Option paper: {e['label']} {e['event']}" + (f" ({e['reason']})" if e.get("reason") else ""))
    if opt_auto:
        start_option_auto(client, now, prices)
    return alerts


_option_task: asyncio.Task | None = None


def start_option_auto(client, now: datetime, prices: dict) -> None:
    """Automatic option orders fetch the option chain of each new signal: that runs as its own background task (one
    at a time), never inside the price round."""
    global _option_task
    if _option_task is not None and not _option_task.done():
        return
    _option_task = asyncio.get_running_loop().create_task(_option_auto(client, now, dict(prices)))


async def _option_auto(client, now: datetime, prices: dict) -> None:
    try:
        with SessionLocal() as db:
            got = await optionpaper.auto_place(db, client, watch_rows(db), now, prices)
        if got["placed"]:
            await hub.broadcast({"type": "option-paper", "event": "order placed"})
    except Exception as exc:                              # shown on the feed status, never stops the price round
        state["option_auto_error"] = f"{type(exc).__name__}: {exc}"


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
