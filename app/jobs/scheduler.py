"""Scheduled jobs (IST, Monday to Friday). Each job does what the matching API does, so anything
the schedule does can also be run by hand. A job that fails is recorded and never stops the app."""
import asyncio
import logging
from datetime import datetime, time, timedelta

from sqlalchemy import delete

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .. import live, settings_store, status
from ..data import sync as data_sync
from ..db import SessionLocal
from ..engine import scanner
from ..alerts import telegram
from ..models import Instrument, Rejection, SyncState
from ..options import chain as options
from ..sim import paper
from .runner import run_job

log = logging.getLogger(__name__)


def _universe(db) -> list[str]:
    return list(settings_store.get(db, "universe") or [])


async def store_instruments(db, client) -> int:
    items = await client.instruments("NSE")
    keep = [i for i in items if i.get("segment") in ("NSE_EQ", "NSE_INDEX", "NSE_FO")]
    db.query(Instrument).delete()
    for n, i in enumerate(keep, 1):
        exp = i.get("expiry")
        db.add(Instrument(instrument_key=i["instrument_key"], symbol=i.get("trading_symbol", ""), name=i.get("name"),
                          segment=i.get("segment"), instrument_type=i.get("instrument_type"), lot_size=i.get("lot_size"),
                          strike=i.get("strike_price"), expiry=datetime.fromtimestamp(exp / 1000) if exp else None,
                          underlying_key=i.get("underlying_key")))
        if n % 5000 == 0:
            db.flush()
    db.commit()
    return len(keep)


async def morning_prep(client):
    async def work(db, report):
        await client.profile()   # raises if the token is missing or expired
        report(f"token valid, {await store_instruments(db, client)} instruments stored")
    return await run_job("morning prep", work)


SYNC_AT_ONCE = 4     # instruments downloaded side by side; the Upstox throttle still caps the request rate


async def each_instrument(keys: list[str], one, report, label: str = "instruments") -> None:
    """Run `one(key)` for every instrument, a few at a time. One failing instrument does not stop the others;
    the first error is raised at the end so the job is recorded as failed."""
    gate, done, errors = asyncio.Semaphore(SYNC_AT_ONCE), 0, []

    async def guarded(key):
        nonlocal done
        async with gate:
            try:
                await one(key)
            except Exception as exc:
                errors.append(f"{key}: {type(exc).__name__}: {exc}")
            done += 1
            report(f"{done} of {len(keys)} {label}" + (f", {len(errors)} failed" if errors else ""))
    await asyncio.gather(*(guarded(k) for k in keys))
    if errors:
        raise RuntimeError(f"{len(errors)} of {len(keys)} failed. First: {errors[0]}")


async def prepare(client, key: str, timeframes: list[str] | None = None, repair: bool = False, retry_if_late: bool = False,
                  now: datetime | None = None, force: bool = True, download: bool = True, full: bool = False) -> None:
    """Take one instrument through download, scan and results, and mark it ready. An instrument that was ready
    before stays visible on the pages while it is being updated; a new one appears when this finishes."""
    def note(state: str, detail: str | None = None):
        with SessionLocal() as d:
            status.mark(d, key, state, detail)
    try:
        if download:
            note("downloading")
            with SessionLocal() as d:      # each instrument has its own session, so several can run side by side
                await data_sync.sync_symbol(d, client, key, timeframes, full=full, repair_days=30 if repair else 0, now=now)
                if retry_if_late and data_sync.needs_sync(d, key, now):
                    await asyncio.sleep(20)   # Upstox publishes an intraday candle a little after the bar ends
                    await data_sync.sync_symbol(d, client, key, timeframes, now=now)
        note("scanning")
        await asyncio.to_thread(scanner.scan_key, key, force, now)      # CPU work: off the main loop
        note("ready")
    except Exception as exc:
        note("failed", f"{type(exc).__name__}: {exc}")
        raise
    await live.hub.broadcast({"type": "ready", "instrument_key": key})   # the pages add this instrument without a reload


async def sync_and_scan(client, job_type: str, timeframes: list[str] | None = None, repair: bool = False,
                        retry_if_late: bool = False, now: datetime | None = None, force: bool = True):
    """Sync every instrument in the stock list, scan it, then tell the pages.
    force=False scans only the timeframes that received a new candle (the five-minute job)."""
    paper_events: list[dict] = []

    async def work(db, report):
        held = {o.instrument_key for o in paper.live_orders(db)} | {t.instrument_key for t in paper.open_trades(db) if t.kind != "option"}
        keys = _universe(db) + sorted(held - set(_universe(db)))   # anything held on paper is kept in step too

        async def one(key: str):
            await prepare(client, key, timeframes, repair, retry_if_late, now, force)
            with SessionLocal() as d:
                paper_events.extend(paper.on_candles(d, key))
        await each_instrument(keys, one, report)
        live.state["last_scan"] = now or data_sync.now_ist()
    job_id = await run_job(job_type, work)
    await live.hub.broadcast({"type": "scan", "time": (now or data_sync.now_ist()).isoformat(timespec="seconds")})
    for e in paper_events:
        await live.hub.broadcast(e)
        telegram.notify(telegram.paper_text(e))
    return job_id


def closed_timeframes(now: datetime) -> list[str]:
    """Fetched timeframes with a bar that closed in the last five minutes. 30-minute bars close at
    :15 and :45, and the last one at 15:30. The 1h and 4h bars are rebuilt from the 30-minute ones."""
    tfs = ["5m"]
    if now.minute % 30 in range(15, 20) or now.time() >= time(15, 30):
        tfs.append("30m")
    return tfs


async def bar_close(client, now: datetime | None = None):
    now = now or data_sync.now_ist()
    if not (time(9, 20) <= now.time() <= time(15, 36)):
        return None
    return await sync_and_scan(client, "bar close", closed_timeframes(now), retry_if_late=True, now=now, force=False)


async def option_snapshots(client, now: datetime | None = None):
    """Save the nearest-expiry option chain for the indices in the stock list and for instruments with a
    watched setup. This is what open interest history and IV percentile are built from."""
    now = now or data_sync.now_ist()
    if not live.market_open(now):
        return None

    async def work(db, report):
        keys = [k for k in _universe(db) if "_INDEX|" in k] + sorted({z.instrument_key for z, _ in live.watch_rows(db)})
        saved = 0
        for key in dict.fromkeys(keys):
            exp = options.expiries(db, key, now.date())
            if not exp:
                continue
            saved += options.save_snapshot(db, key, exp[0], await client.option_chain(key, exp[0]), now)
        report(f"{saved} option rows saved")
    return await run_job("options", work)


async def health(client):
    """Repair recent gaps, and clear out old log rows."""
    async def work(db, report):
        gappy = sorted({s.instrument_key for s in db.query(SyncState).filter(SyncState.gaps > 0)})
        for key in gappy:
            await data_sync.sync_symbol(db, client, key, repair_days=30)
        now = data_sync.now_ist()
        db.execute(delete(Rejection).where(Rejection.ts < now - timedelta(days=90)))
        options.prune(db, now)
        db.commit()
        gone = await asyncio.to_thread(prune_old_zones)
        report(f"{len(gappy)} instruments repaired, {gone} old used-up zones removed, old log rows cleared")
    return await run_job("health", work)


def prune_old_zones() -> int:
    """Remove zones that were used up long ago. Own session: runs in a worker thread. A clean-up only: if the database
    is busy (a table being emptied or locked), it is skipped and simply runs again next time."""
    from sqlalchemy.exc import OperationalError
    with SessionLocal() as db:
        try:
            return scanner.prune_closed(db)
        except OperationalError as exc:
            db.rollback()
            log.warning("old zone clean-up skipped, the database was busy: %s", exc.orig)
            return 0


def build(client) -> AsyncIOScheduler:
    sch = AsyncIOScheduler(timezone="Asia/Kolkata", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120})
    days = "mon-fri"
    sch.add_job(morning_prep, CronTrigger(day_of_week=days, hour=8, minute=45), args=[client], id="morning prep")
    sch.add_job(sync_and_scan, CronTrigger(day_of_week=days, hour=9, minute=0), args=[client, "catch-up"], id="catch-up")
    # one minute and fifteen seconds after each 5-minute bar ends, 09:21 to 15:31
    sch.add_job(bar_close, CronTrigger(day_of_week=days, hour="9-15", minute="1-59/5", second=15), args=[client], id="bar close")
    sch.add_job(sync_and_scan, CronTrigger(day_of_week=days, hour=15, minute=40), args=[client, "end of day"], id="end of day")
    sch.add_job(option_snapshots, CronTrigger(day_of_week=days, hour="9-15", minute="3-59/5"), args=[client], id="options")
    sch.add_job(health, CronTrigger(day_of_week=days, hour=16, minute=0), args=[client], id="health")
    return sch