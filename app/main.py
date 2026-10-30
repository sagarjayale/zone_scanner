"""FastAPI app. Phases 1 to 3: login, data sync, zone engine and scan APIs.
Open /docs to call every endpoint by hand."""
import asyncio
from types import SimpleNamespace
from contextlib import asynccontextmanager
import csv
import io
from datetime import date, datetime

from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

import os

from . import config, live, settings_store, status
from .alerts import telegram
from .data import indices
from .data import sync as data_sync
from .db import SessionLocal, engine, ensure_database, ensure_schema, get_db
from .engine import plan as plans
from .engine import confirm
from .engine import result as results
from .jobs import scheduler as jobs
from .jobs.runner import run_job, start_job
from .engine import scanner
from .engine.link import ACTIVE
from .models import (BacktestRun, BacktestTrade, Candle, Instrument, Job, ModeResult, PaperOrder, PaperTrade, Rejection, Setting,
                     SyncState, TradePlan, Zone, ZoneEvent)
from .options import chain as options
from .sim import backtest as bt
from .sim import scancheck
from .sim import paper
from .sim.replay import SESSIONS, ReplaySession
from .upstox.client import TokenMissing, UpstoxClient, UpstoxError


def current_token() -> str:
    with SessionLocal() as db:
        tok = settings_store.get(db, "access_token")
    return tok or config.ENV_TOKEN


client = UpstoxClient(current_token)
# GTF-2: trade score and entry types 1, 2 and 3 from the course; a close inside a zone no longer ends it.
# GTF-3: zone marking as in the course: exciting leg-in, leg-out closing beyond the whole leg-in, exceptional distal line, gap candles.
# GTF-4: zones found, tested, used up and scored as scanner.py v6.4 does, with its history windows (the course marking stays a setting).
# GTF-5: the nearest HTF demand and supply must never have been tested (GTF PDF page 31); results and plans are rebuilt.
RULES_VERSION = "GTF-5"


RESET_TABLES = (ModeResult, ZoneEvent, TradePlan, Zone, Rejection)   # what a change of rules clears; candles are kept


def clear_zone_tables(report=lambda text: None, tries: int = 40, pause: float = 15.0) -> None:
    """Empty the tables a change of rules makes stale. On MySQL with TRUNCATE, which takes a moment whatever the size,
    waiting at most 30 seconds for a lock each time; if another connection holds the table, it waits and tries again
    (up to ten minutes) and says so on the job. On SQLite with DELETE."""
    import time as _time
    from sqlalchemy import text as sql
    from sqlalchemy.exc import OperationalError
    for model in RESET_TABLES:
        name = model.__tablename__
        for attempt in range(1, tries + 1):
            try:
                with engine.begin() as conn:
                    if engine.dialect.name == "mysql":
                        conn.execute(sql("SET SESSION lock_wait_timeout = 30, innodb_lock_wait_timeout = 30"))
                        try:
                            conn.execute(sql(f"TRUNCATE TABLE `{name}`"))
                        finally:
                            conn.execute(sql("SET SESSION lock_wait_timeout = DEFAULT, innodb_lock_wait_timeout = DEFAULT"))
                    else:
                        conn.execute(delete(model))
                break
            except OperationalError:
                if attempt == tries:
                    raise
                report(f"waiting for the {name} table: another database connection is holding it (try {attempt} of {tries}). "
                       "Stop any other copy of the app.")
                _time.sleep(pause)
        report(f"cleared {name}")


def reset_zones(db: Session, earlier: str | None = None) -> None:
    """Zones found by an earlier rule set do not carry what the current rules need, so they are cleared once.
    Candles are kept, and every zone is found again from them. Pending paper orders lose their zone and are cancelled.
    The tables are emptied by the background job (clear_zone_tables), never during start-up; here only the small things."""
    for order in db.scalars(select(PaperOrder).where(PaperOrder.status == "pending")).all():
        order.status, order.note = "cancelled", "zone rules changed"
    stale = ["strong_body_pct", "conservative_rr"]          # settings the current rules no longer have
    if earlier in (None, "GTF-1", "GTF-2", "GTF-3"):        # GTF-4 made scanner.py the default marking: earlier choices go back to it
        stale.append("marking_rule")
    if not (earlier or "").startswith("GTF"):              # thresholds saved before the GTF rules would be misread by them
        stale += ["leg_atr_mult", "max_width_atr", "stop_buffer_atr", "min_rr", "max_rr", "atr_period", "approach_atr",
                  "swing_bars", "move_open_targets", "min_score", "leg_body_pct", "base_body_pct"]
    for key in stale:
        row = db.get(Setting, key)
        if row is not None:
            db.delete(row)
    db.commit()


async def rescan_all() -> None:
    """After the rules changed: empty the zone tables, then find every zone again from the stored candles, in the
    background. No download is needed. The setting rules_reset_pending stays on until this has finished, so an app that
    was stopped half way does it again at the next start."""
    async def work(d, report):
        report("clearing the zones of the earlier rules")
        await asyncio.to_thread(clear_zone_tables, report)
        with SessionLocal() as db:
            for st in db.scalars(select(SyncState)).all():
                st.last_scanned_ts = None
            for model in (ZoneEvent, TradePlan, Zone):            # anything a scheduled scan wrote meanwhile
                db.execute(delete(model))
            db.commit()
            keys = universe(db)
        if keys:
            await jobs.each_instrument(keys, lambda key: jobs.prepare(client, key, download=False), report)
        with SessionLocal() as db:
            row = db.get(Setting, "rules_reset_pending")
            if row is not None:
                db.delete(row)
                db.commit()
    await run_job("rebuild after a rule change", work)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_database()
    ensure_schema()
    with SessionLocal() as db:
        earlier = settings_store.get(db, "engine_rules")
        if earlier != RULES_VERSION:            # only small changes here; the tables are emptied in the background
            reset_zones(db, earlier)
            settings_store.put(db, "rules_reset_pending", True)
            settings_store.put(db, "engine_rules", RULES_VERSION)
        rebuilt = bool(settings_store.get(db, "rules_reset_pending"))
        status.adopt_existing(db, list(settings_store.get(db, "universe") or []))
    sched = None
    if os.getenv("SCHEDULER_ENABLED", "1") == "1":   # set to 0 to run the pages and APIs without the schedule
        sched = jobs.build(client)
        sched.start()
        live.start_poller(client)
    app.state.scheduler = sched
    if rebuilt:     # the zones were cleared for the new rules: find them again right away, nothing waits for it
        asyncio.get_running_loop().create_task(rescan_all())
    if sched and not rebuilt:   # zones used up long ago are cleared in the background (not while the rebuild empties the tables)
        asyncio.get_running_loop().create_task(asyncio.to_thread(jobs.prune_old_zones))
    yield
    if sched:
        sched.shutdown(wait=False)
    task = live.state.get("task")
    if task:
        task.cancel()
    await client.close()


app = FastAPI(title="Zone scanner", version="0.4", lifespan=lifespan)
WEB = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/scanner")


@app.exception_handler(TokenMissing)
async def _no_token(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=401)


@app.exception_handler(UpstoxError)
async def _upstox_failed(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.get("/paper", include_in_schema=False)
def paper_page():
    return FileResponse(WEB / "paper.html")


@app.get("/options", include_in_schema=False)
def options_page():
    return FileResponse(WEB / "options.html")


@app.get("/settings", include_in_schema=False)
def settings_page():
    return FileResponse(WEB / "settings.html")


@app.get("/scanner", include_in_schema=False)
def scanner_page():
    return FileResponse(WEB / "scanner.html")


@app.get("/chart", include_in_schema=False)
def chart_page():
    return FileResponse(WEB / "chart.html")


@app.get("/backtest", include_in_schema=False)
def backtest_page():
    return FileResponse(WEB / "backtest.html")


@app.get("/data", include_in_schema=False)
def data_page():
    return FileResponse(WEB / "data.html")


def row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def resolve(db: Session, symbol: str) -> str:
    """Accepts an instrument key (NSE_EQ|INE...) or a trading symbol (RELIANCE, NIFTY)."""
    if "|" in symbol:
        return symbol
    inst = db.scalars(select(Instrument).where(Instrument.symbol == symbol.upper(),
                                               Instrument.segment.in_(("NSE_EQ", "NSE_INDEX")))).first()
    if not inst:
        raise HTTPException(404, f"Unknown symbol {symbol}. Refresh instruments or pass the instrument key.")
    return inst.instrument_key


def universe(db: Session) -> list[str]:
    return list(settings_store.get(db, "universe") or [])


# ---------------------------------------------------------------- auth (3)
@app.get("/auth/login", tags=["auth"])
def auth_login():
    if not config.API_KEY:
        raise HTTPException(400, "UPSTOX_API_KEY is not set in .env")
    return RedirectResponse(client.login_url())


@app.get("/auth/callback", tags=["auth"])
async def auth_callback(code: str, db: Session = Depends(get_db)):
    try:
        token = await client.exchange_code(code)
    except UpstoxError as exc:
        raise HTTPException(400, str(exc))
    settings_store.put(db, "access_token", token)
    settings_store.put(db, "token_time", data_sync.now_ist().isoformat())
    return {"status": "logged in"}


@app.get("/api/auth/status", tags=["auth"])
async def auth_status(check: bool = False, db: Session = Depends(get_db)):
    """check=true makes one Upstox call to confirm the token still works."""
    has = bool(current_token())
    out = {"has_token": has, "token_time": settings_store.get(db, "token_time"), "valid": None}
    if has and check:
        try:
            await client.profile()
            out["valid"] = True
        except UpstoxError:
            out["valid"] = False
    return out


# ---------------------------------------------------------------- data (7)
def _keys(db: Session, symbols) -> list[str]:
    if symbols in (None, "all"):
        return universe(db)
    return [resolve(db, s) for s in symbols]


@app.post("/api/data/sync", tags=["data"])
async def data_sync_many(symbols: list[str] | str = Body("all"), timeframes: list[str] | None = Body(None),
                         repair: bool = Body(False), db: Session = Depends(get_db)):
    """Download what is missing and scan it, one instrument after another in the background. Each instrument
    becomes available on the pages as soon as it is done; the rest carry on. repair=true also re-fetches the
    last 30 days of intraday candles to fill gaps."""
    keys = _keys(db, symbols)

    async def work(d, report):
        await jobs.each_instrument(keys, lambda key: jobs.prepare(client, key, timeframes, repair), report)
    return {"job_id": start_job("sync and scan", work), "symbols": len(keys)}


@app.post("/api/data/backfill", tags=["data"])
async def data_backfill(symbols: list[str] | str = Body("all", embed=True), db: Session = Depends(get_db)):
    """Full history download, ignoring the sync marker, followed by a scan."""
    keys = _keys(db, symbols)

    async def work(d, report):
        await jobs.each_instrument(keys, lambda key: jobs.prepare(client, key, full=True), report)
    return {"job_id": start_job("full download", work), "symbols": len(keys)}


@app.post("/api/data/sync/{symbol:path}", tags=["data"])
async def data_sync_one(symbol: str, db: Session = Depends(get_db)):
    key = resolve(db, symbol)
    try:
        return {"instrument_key": key, "written": await data_sync.sync_symbol(db, client, key)}
    except TokenMissing as exc:
        raise HTTPException(401, str(exc))
    except UpstoxError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/data/status", tags=["data"])
def data_status(db: Session = Depends(get_db)):
    counts = dict(((k, tf), n) for k, tf, n in db.execute(
        select(Candle.instrument_key, Candle.timeframe, func.count()).group_by(Candle.instrument_key, Candle.timeframe)))
    states = db.scalars(select(SyncState).order_by(SyncState.instrument_key)).all()
    return {"api_calls": client.throttle.used(), "status": status.overview(db, universe(db)),
            "rows": [{**row(s), "candles": counts.get((s.instrument_key, s.timeframe), 0)} for s in states]}


@app.get("/api/candles/{symbol:path}", tags=["data"])
def candles(symbol: str, tf: str = "1d", start: datetime | None = None, end: datetime | None = None,
            limit: int = Query(500, le=8000), first: bool = False, db: Session = Depends(get_db)):
    """The newest `limit` candles in the range, or with first=true the earliest ones (used to play forward from a date)."""
    if tf not in config.TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {config.TIMEFRAMES}")
    if first:
        df = data_sync.load_candles(db, resolve(db, symbol), tf, since=start, until=end).head(limit)
    else:
        df = data_sync.load_candles(db, resolve(db, symbol), tf, since=start, until=end, last=limit)
    df["ts"] = df["ts"].astype(str) if not df.empty else df["ts"]
    return df.to_dict("records")


@app.post("/api/instruments/refresh", tags=["data"])
async def instruments_refresh():
    async def work(d, report):
        report(f"{await jobs.store_instruments(d, client)} instruments stored")
    return {"job_id": start_job("instruments", work)}


@app.get("/api/jobs/{job_id}", tags=["data"])
def job_status(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return row(job)


# ---------------------------------------------------------------- scan (6)
@app.post("/api/scan/run", tags=["scan"])
async def scan_run(db: Session = Depends(get_db)):
    """Scan candles not yet scanned, for every instrument in the stock list."""
    keys = universe(db)

    async def work(d, report):
        await jobs.each_instrument(keys, lambda key: jobs.prepare(client, key, download=False), report)
    return {"job_id": start_job("scan", work), "symbols": len(keys)}


@app.post("/api/scan/{symbol:path}", tags=["scan"])
async def scan_one(symbol: str, sync: bool = True, db: Session = Depends(get_db)):
    """Sync this instrument if a bar has closed since the last stored candle, then scan it."""
    key = resolve(db, symbol)
    synced = None
    if sync and data_sync.needs_sync(db, key):
        try:
            synced = await data_sync.sync_symbol(db, client, key)
        except TokenMissing as exc:
            raise HTTPException(401, str(exc))
        except UpstoxError as exc:
            raise HTTPException(502, str(exc))
    stats = await asyncio.to_thread(scanner.scan_key, key, True)
    status.mark(db, key, "ready")
    return {"instrument_key": key, "synced": synced, "scan": stats}


def _setup_row(z: Zone, p: TradePlan, names: dict, triggered_ts=None) -> dict:
    ctx = p.context or {}
    return {"zone_id": z.id, "plan_id": p.id, "instrument_key": z.instrument_key,
            "symbol": names.get(z.instrument_key) or z.instrument_key.split("|")[-1], "is_index": "_INDEX|" in z.instrument_key,
            "mode": p.mode, "timeframe": z.timeframe, "side": z.side, "signal": "BUY" if z.side == "demand" else "SELL",
            "pattern": z.pattern, "proximal": z.proximal, "distal": z.distal, "score": z.score, "label": z.label,
            "status": z.status, "wick_tests": z.touches, "touches": z.touches, "base_count": z.base_count, "has_gap": bool(z.has_gap),
            "created_ts": z.created_ts, "plan_status": p.status, "plan_version": p.version, "zone_state": p.zone_state,
            "entry": p.entry, "stop": p.stop, "target": p.target, "reward_risk": p.reward_risk, "plans": p.plans,
            "curve": ctx.get("curve"), "htf_trend": ctx.get("htf_trend"), "itf_trend": ctx.get("itf_trend"),
            "market_state": ctx.get("market_state"), "htf": ctx.get("htf"), "itf": ctx.get("itf"),
            "htf_demand": ctx.get("demand"), "htf_supply": ctx.get("supply"), "allowed": p.allowed,
            "contact_ltf": (p.shared or {}).get("ltf", []) if isinstance(p.shared, dict) else [],
            "contact_htf": (p.shared or {}).get("htf", []) if isinstance(p.shared, dict) else [],
            "triggered_ts": triggered_ts, "price": live.state["prices"].get(z.instrument_key)}


@app.get("/api/setups", tags=["scan"])
def setups(mode: str | None = None, timeframe: str | None = None, side: str | None = None, min_score: float | None = None,
           symbol: str | None = None, include_triggered: bool = False, contact: str | None = None,
           db: Session = Depends(get_db)):
    """Untouched execution zones with a pending plan: the HTF curve and ITF trend of some mode allow the trade.
    A zone can appear once per mode. include_triggered adds setups whose entry was reached today.
    min_score 7 keeps Type 1 zones only; 5 keeps Type 2 and 3 as well. timeframe is the execution zone's timeframe.
    contact keeps setups whose LTF or HTF zone is in contact with a higher timeframe ("any", or one timeframe)."""
    floor = min_score if min_score is not None else settings_store.get(db, "min_score")

    def narrowed(q):
        if mode:
            q = q.where(TradePlan.mode == mode.upper())
        if timeframe:
            q = q.where(Zone.timeframe == timeframe)
        if side:
            q = q.where(Zone.side == side)
        if symbol:
            q = q.where(Zone.instrument_key == resolve(db, symbol))
        return q.where(TradePlan.is_current.is_(True), TradePlan.mode.isnot(None))

    base = select(Zone, TradePlan).join(TradePlan, TradePlan.zone_id == Zone.id)
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(Instrument.segment.in_(("NSE_EQ", "NSE_INDEX")))).all())
    rows = db.execute(narrowed(base.where(Zone.status == "fresh", TradePlan.status == "pending", Zone.score >= floor))
                      .order_by(Zone.score.desc(), Zone.created_ts.desc())).all()
    out = [_setup_row(z, p, names) for z, p in rows]
    if include_triggered:
        today = datetime.combine(data_sync.now_ist().date(), datetime.min.time())
        hits = dict(db.execute(select(ZoneEvent.zone_id, func.min(ZoneEvent.ts)).where(
            ZoneEvent.event_type.in_(("alert_triggered", "touched")), ZoneEvent.ts >= today).group_by(ZoneEvent.zone_id)).all())
        if hits:
            q = narrowed(base.where(Zone.id.in_(list(hits)), TradePlan.status == "triggered"))
            out += [_setup_row(z, p, names, hits[z.id]) for z, p in db.execute(q).all()]
    if contact:      # "any": some higher timeframe is in contact; or a timeframe such as "1d": that one is
        if contact != "any" and contact not in config.TIMEFRAMES:
            raise HTTPException(400, "contact must be 'any' or a timeframe.")
        tfs = lambda r: {c["tf"] for c in r["contact_ltf"] + r["contact_htf"]}
        out = [r for r in out if (tfs(r) if contact == "any" else contact in tfs(r))]
    return out


@app.get("/api/zones/{symbol:path}", tags=["scan"])
def zones(symbol: str, timeframe: str | None = None, active_only: bool = False, inactive_only: bool = False,
          limit: int = Query(2000, le=5000), db: Session = Depends(get_db)):
    """Zones of one instrument, newest first. active_only: fresh and tested zones. inactive_only: violated and used-up zones."""
    q = select(Zone).where(Zone.instrument_key == resolve(db, symbol))
    if timeframe:
        q = q.where(Zone.timeframe == timeframe)
    if active_only:
        q = q.where(Zone.status.in_(ACTIVE))
    if inactive_only:
        q = q.where(Zone.status.notin_(ACTIVE))
    return [row(z) for z in db.scalars(q.order_by(Zone.created_ts.desc()).limit(limit)).all()]


@app.get("/api/zone/{zone_id}", tags=["scan"])
def zone_detail(zone_id: int, db: Session = Depends(get_db)):
    z = db.get(Zone, zone_id)
    if not z:
        raise HTTPException(404, "No such zone")
    events = db.scalars(select(ZoneEvent).where(ZoneEvent.zone_id == zone_id).order_by(ZoneEvent.ts)).all()
    plans = db.scalars(select(TradePlan).where(TradePlan.zone_id == zone_id).order_by(TradePlan.version)).all()
    return {"zone": row(z), "events": [row(e) for e in events], "plans": [row(p) for p in plans]}


@app.get("/api/rejections", tags=["scan"])
def rejections(symbol: str | None = None, timeframe: str | None = None, limit: int = Query(200, le=2000),
               db: Session = Depends(get_db)):
    q = select(Rejection)
    if symbol:
        q = q.where(Rejection.instrument_key == resolve(db, symbol))
    if timeframe:
        q = q.where(Rejection.timeframe == timeframe)
    return [row(r) for r in db.scalars(q.order_by(Rejection.ts.desc()).limit(limit)).all()]


# ---------------------------------------------------------------- settings (2)
HIDDEN_KEYS = {"access_token", "token_time", "telegram_token"}   # never returned by the API
READ_ONLY_KEYS = {"symbols", "telegram_configured", "sync_job_id", "ready", "status", "engine_rules", "rules_reset_pending"}   # computed values, ignored if sent back


@app.get("/api/settings", tags=["system"])
def settings_get(db: Session = Depends(get_db)):
    out = dict(config.DEFAULTS)
    for s in db.scalars(select(Setting)).all():
        if s.key not in HIDDEN_KEYS:
            out[s.key] = s.value
    keys = out.get("universe") or []
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(Instrument.instrument_key.in_(keys))).all())
    out["symbols"] = {k: names.get(k) or k.split("|")[-1] for k in keys}
    out["ready"] = status.ready_keys(db, keys)            # the pages offer only these
    out["status"] = status.overview(db, keys)
    out["telegram_configured"] = telegram.configured()
    return out


@app.put("/api/settings", tags=["system"])
async def settings_put(values: dict = Body(...), db: Session = Depends(get_db)):
    """Save thresholds, the stock list (key 'universe'), per-timeframe overrides (key 'thresholds:<tf>'),
    paper trading settings, or the Telegram bot token and chat id. Instruments new to the stock list are
    synced and scanned in the background."""
    if "direction_rule" in values and values["direction_rule"] not in ("location", "trend"):
        raise HTTPException(400, "direction_rule must be 'location' or 'trend'.")
    if "marking_rule" in values and values["marking_rule"] not in ("scanner", "pdf"):
        raise HTTPException(400, "marking_rule must be 'scanner' or 'pdf'.")
    if "htf_zone_pick" in values and values["htf_zone_pick"] not in ("fresh", "any"):
        raise HTTPException(400, "htf_zone_pick must be 'fresh' or 'any'.")
    if "zone_history" in values and values["zone_history"] not in ("scanner", "all"):
        raise HTTPException(400, "zone_history must be 'scanner' or 'all'.")
    if "entry_23" in values and values["entry_23"] not in ("TYPE_2", "TYPE_3"):
        raise HTTPException(400, "entry_23 must be 'TYPE_2' or 'TYPE_3'.")
    before = set(universe(db))
    for k, v in values.items():
        if k in ("access_token", "token_time"):
            raise HTTPException(400, f"{k} cannot be set here. Use Log in with Upstox.")
        if k in READ_ONLY_KEYS:
            continue
        settings_store.put(db, k, v)
    added = [k for k in universe(db) if k not in before]
    job_id = None
    if added:
        for key in added:
            if not status.overview(db, [key])[key]["ready"]:
                status.mark(db, key, "queued")

        async def work(d, report):
            await jobs.each_instrument(added, lambda key: jobs.prepare(client, key), report, "new instruments ready")
        job_id = start_job("new instruments", work)
    return {**settings_get(db), "sync_job_id": job_id}


@app.get("/api/instruments/search", tags=["system"])
def instruments_search(q: str = Query(..., min_length=2), db: Session = Depends(get_db)):
    """Find stocks and indices by symbol or name, for the stock list."""
    like = f"%{q.strip()}%"
    rows = db.scalars(select(Instrument).where(Instrument.segment.in_(("NSE_EQ", "NSE_INDEX")),
                                               Instrument.symbol.ilike(like) | Instrument.name.ilike(like))
                      .order_by(func.length(Instrument.symbol)).limit(20)).all()
    return [{"instrument_key": i.instrument_key, "symbol": i.symbol, "name": i.name, "segment": i.segment} for i in rows]


@app.get("/api/instruments/index/{name}", tags=["system"])
async def instruments_index(name: str, db: Session = Depends(get_db)):
    """Current members of an index (nifty50, niftynext50, niftybank, niftymidcap100), read from the official
    NSE Indices file and matched to Upstox instruments, ready to add to the stock list."""
    if not db.scalar(select(func.count()).select_from(Instrument)):
        raise HTTPException(400, "The instruments file has not been downloaded yet. Use Refresh instruments on the Data page first.")
    try:
        text, url = await indices.fetch_csv(name)
    except indices.IndexListError as exc:
        raise HTTPException(502, str(exc))
    rows = indices.match(db, indices.parse_csv(text), set(universe(db)))
    return {"index": indices.INDICES[name][0], "source": url, "count": len(rows), "rows": rows}


@app.post("/api/instruments/match", tags=["system"])
def instruments_match(text: str = Body(..., embed=True), db: Session = Depends(get_db)):
    """Match pasted symbols or ISINs to Upstox instruments, ready to add to the stock list."""
    wanted = indices.parse_pasted(text)
    if not wanted:
        raise HTTPException(400, "Type or paste at least one symbol.")
    if len(wanted) > 300:
        raise HTTPException(400, "That is more than 300 symbols. Add them in smaller groups.")
    rows = indices.match(db, wanted, set(universe(db)))
    return {"index": "Pasted symbols", "source": None, "count": len(rows), "rows": rows}


@app.get("/api/jobs", tags=["system"])
def jobs_list(limit: int = Query(30, le=200), db: Session = Depends(get_db)):
    return [row(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(limit)).all()]


@app.post("/api/alerts/test", tags=["system"])
async def alerts_test():
    ok, detail = await telegram.send("Zone scanner: test alert. Telegram is set up correctly.")
    if not ok:
        raise HTTPException(400, detail)
    return {"sent": True}


@app.post("/api/rebuild/zones", tags=["scan"])
async def scan_rebuild(db: Session = Depends(get_db)):
    """Clear all zones, plans and rejections for the stock list and scan the stored candles again.
    Use it after changing thresholds. Candles are kept; paper positions are kept."""
    keys = universe(db)

    def rebuild(key: str):
        with SessionLocal() as own:
            scanner.clear_key(own, key)
        return scanner.scan_key(key, True)

    async def work(d, report):
        for n, key in enumerate(keys, 1):
            await asyncio.to_thread(rebuild, key)
            status.mark(d, key, "ready")
            report(f"{n} of {len(keys)} instruments rebuilt")
    return {"job_id": start_job("rebuild zones", work), "symbols": len(keys)}


# ---------------------------------------------------------------- replay (2)
@app.post("/api/replay/start", tags=["replay"])
def replay_start(symbol: str = Body(...), timeframe: str = Body("1d"), start: datetime = Body(...),
                 db: Session = Depends(get_db)):
    """Start a replay session from a date. Runs on stored candles in a separate copy of the engine."""
    if timeframe not in config.TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {config.TIMEFRAMES}")
    try:
        session = ReplaySession(db, resolve(db, symbol), timeframe, start)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"session_id": session.id, "remaining": session.remaining}


@app.websocket("/ws/replay/{session_id}")
async def replay_ws(ws: WebSocket, session_id: str):
    """Commands: {"cmd": "play", "speed": bars per second}, "pause", "step", "next_event"."""
    await ws.accept()
    session = SESSIONS.get(session_id)
    if session is None:
        await ws.send_json({"type": "error", "message": "Replay session not found. Start a new one."})
        await ws.close()
        return
    await ws.send_json(session.init_payload())
    playing, delay = False, 0.5
    try:
        while True:
            send = 0
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=delay if playing else None)
                cmd = msg.get("cmd")
                if cmd == "play":
                    playing, delay = True, 1 / max(0.5, min(60, float(msg.get("speed", 2))))
                elif cmd == "pause":
                    playing = False
                elif cmd == "step":
                    playing, send = False, 1
                elif cmd == "next_event":
                    playing, send = False, -1
            except asyncio.TimeoutError:
                send = 1
            while send:
                bar = session.next_bar()
                if bar is None:
                    playing = False
                    await ws.send_json({"type": "end"})
                    break
                await ws.send_json(bar)
                send = 0 if send == 1 or bar["events"] else send
    except WebSocketDisconnect:
        pass
    finally:
        SESSIONS.pop(session_id, None)


# ---------------------------------------------------------------- backtest (5)
@app.post("/api/backtest/run", tags=["backtest"])
async def backtest_run(symbols: list[str] | str = Body("all"), modes: list[str] = Body(["DAILY"]),
                       start: date = Body(...), end: date = Body(...), min_score: float | None = Body(None),
                       cost_pct: float = Body(0.05), entry_plan: str = Body("rule"), db: Session = Depends(get_db)):
    """Start a backtest on stored candles. modes are GTF analysis modes (each brings its own HTF, ITF and LTF).
    entry_plan is rule (the entry the trade score takes), type1, type2 or type3. cost_pct is charged on each side of a trade."""
    keys = _keys(db, symbols)
    modes = [m.upper() for m in modes]
    if not modes or any(m not in config.MODES for m in modes):
        raise HTTPException(400, f"Modes must be chosen from {list(config.MODES)}")
    if entry_plan not in bt.ENTRY_PLANS:
        raise HTTPException(400, f"entry_plan must be one of {list(bt.ENTRY_PLANS)}")
    if not keys:
        raise HTTPException(400, "No instruments chosen.")
    if end < start:
        raise HTTPException(400, "The end date is before the start date.")
    have = {(k, tf) for k, tf in db.execute(select(Candle.instrument_key, Candle.timeframe).distinct())}
    missing = sorted({f"{k} {tf}" for k in keys for m in modes for tf in config.MODES[m] if (k, tf) not in have})
    if missing:
        raise HTTPException(400, "No candles stored for: " + ", ".join(missing[:8]) + ". Sync them from the Data page first.")
    run = BacktestRun(status="running", created_at=data_sync.now_ist(), progress="starting",
                      settings={"symbols": keys, "modes": modes, "start": str(start), "end": str(end),
                                "min_score": min_score if min_score is not None else settings_store.get(db, "min_score"),
                                "cost_pct": cost_pct, "entry_plan": entry_plan})
    db.add(run)
    db.commit()
    asyncio.get_running_loop().create_task(asyncio.to_thread(bt.execute, run.id))
    return {"run_id": run.id}


def _run_head(r: BacktestRun) -> dict:
    s = r.summary or {}
    return {"id": r.id, "status": r.status, "progress": r.progress, "error": r.error, "created_at": r.created_at,
            "ended_at": r.ended_at, "settings": r.settings,
            "headline": {k: s.get(k) for k in ("trades", "win_rate", "expectancy", "profit_factor", "max_drawdown_r", "total_r")}}


@app.get("/api/backtest/runs", tags=["backtest"])
def backtest_runs(db: Session = Depends(get_db)):
    return [_run_head(r) for r in db.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(100)).all() if not _is_scan(r)][:50]


@app.get("/api/backtest/{run_id}", tags=["backtest"])
def backtest_get(run_id: int, db: Session = Depends(get_db)):
    r = db.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, "No such backtest run")
    trades = db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_ts)).all()
    return {**_run_head(r), "summary": r.summary, "trades": [row(t) for t in trades]}


@app.post("/api/backtest/{run_id}/cancel", tags=["backtest"])
def backtest_cancel(run_id: int, db: Session = Depends(get_db)):
    r = db.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, "No such backtest run")
    if r.status == "running":
        bt.CANCEL.add(run_id)
    return {"id": run_id, "status": r.status}


@app.get("/api/backtest/{run_id}/export", tags=["backtest"])
def backtest_export(run_id: int, db: Session = Depends(get_db)):
    trades = db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_ts)).all()
    if not trades:
        raise HTTPException(404, "This run has no trades to export.")
    cols = [c.name for c in BacktestTrade.__table__.columns if c.name not in ("id", "run_id")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for t in trades:
        w.writerow([getattr(t, c) for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="backtest_{run_id}.csv"'})


# ---------------------------------------------------------------- live (3)
ALERT_TYPES = ("alert_approaching", "alert_triggered")


@app.get("/api/live/status", tags=["live"])
def live_status(mode: str = "DAILY", db: Session = Depends(get_db)):
    """Everything the scanner page shows around the table: market and feed state, what each index says in the
    chosen mode (curve, trends and trade side), the last alerts, the last scan time and the next bar close."""
    now = data_sync.now_ist()
    mode = mode.upper() if mode.upper() in config.MODES else "DAILY"
    ltf = config.MODES[mode][2]
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol)
                            .where(Instrument.instrument_key.in_(universe(db)))).all())
    bias = []
    for key in universe(db):
        if "_INDEX|" not in key:
            continue
        price = live.state["prices"].get(key)
        if price is None:
            last = data_sync.load_candles(db, key, ltf, last=1)
            price = float(last["close"].iloc[-1]) if not last.empty else None
        row_ = {"instrument_key": key, "symbol": names.get(key) or key.split("|")[-1], "price": price,
                "curve": None, "itf_trend": None, "htf_trend": None, "market_state": None, "side": "NONE"}
        if price is not None:
            ctx = plans.mode_context(db, key, mode, price, now)
            row_.update({k: ctx[k] for k in ("curve", "itf_trend", "htf_trend", "market_state", "side")})
        bias.append(row_)
    alert_zone_ids = select(ZoneEvent.zone_id).where(ZoneEvent.event_type.in_(ALERT_TYPES))
    events = db.execute(select(ZoneEvent, Zone).join(Zone, Zone.id == ZoneEvent.zone_id).where(
        ZoneEvent.event_type.in_(ALERT_TYPES) | (ZoneEvent.event_type.in_(("violated", "consumed", "entry2", "entry3")) & ZoneEvent.zone_id.in_(alert_zone_ids)))
        .order_by(ZoneEvent.ts.desc(), ZoneEvent.id.desc()).limit(20)).all()
    alerts = [{"time": e.ts, "alert": e.event_type.replace("alert_", ""), "zone_id": z.id, "instrument_key": z.instrument_key,
               "symbol": names.get(z.instrument_key) or z.instrument_key.split("|")[-1], "timeframe": z.timeframe,
               "side": z.side, "price": e.price, "proximal": z.proximal} for e, z in events]
    return {"now": now, "market_open": live.market_open(now), "has_token": bool(current_token()),
            "scheduler": app.state.scheduler is not None, "feed": live.feed_status(now), "last_scan": live.state["last_scan"],
            "next_bar_close": live.next_bar_close(now), "bias_mode": mode, "bias": bias, "alerts": alerts,
            "approach_pct": settings_store.get(db, "approach_pct"), "modes": {m: list(v) for m, v in config.MODES.items()},
            "prices": live.state["prices"]}


@app.post("/api/feed/reconnect", tags=["live"])
async def feed_reconnect():
    """Restart the live price poller."""
    live.start_poller(client)
    return {"running": True}


@app.websocket("/ws/live")
async def live_ws(ws: WebSocket):
    """Pushes prices, alerts and scan notices to the pages."""
    await ws.accept()
    live.hub.clients.add(ws)
    try:
        if live.state["prices"]:
            await ws.send_json({"type": "prices", "data": live.state["prices"], "time": None})
        while True:
            await ws.receive_text()   # the page sends nothing; this just notices a disconnect
    except WebSocketDisconnect:
        pass
    finally:
        live.hub.clients.discard(ws)


# ---------------------------------------------------------------- paper trading (7)
def _paper_error(exc: ValueError):
    raise HTTPException(400, str(exc))


def _trade_view(t: PaperTrade, names: dict) -> dict:
    price = live.state["prices"].get(t.instrument_key)
    sign = 1 if t.side == "demand" else -1
    out = row(t)
    out.update(symbol=t.label or names.get(t.instrument_key) or t.instrument_key.split("|")[-1], price=price,
               open_pnl=round((price - t.fill_price) * sign * t.quantity, 2) if price is not None and t.exit_ts is None else None)
    return out


@app.post("/api/paper/orders", tags=["paper"])
async def paper_order(zone_id: int | None = Body(None), option_key: str | None = Body(None), label: str | None = Body(None),
                      lots: int = Body(1), mode: str | None = Body(None), db: Session = Depends(get_db)):
    """Create a pending paper order from a zone's current plan, or (with option_key) open a paper option
    position at the live premium. Nothing is sent to Upstox."""
    now = data_sync.now_ist()
    try:
        if option_key:
            price = (await client.ltp([option_key])).get(option_key)
            if not price:
                raise HTTPException(400, "Upstox returned no live price for this option, so it cannot be paper traded now.")
            return row(paper.open_option(db, option_key, label or option_key, price, lots, now))
        if zone_id is None:
            raise HTTPException(400, "Give a zone_id or an option_key.")
        order = paper.place(db, zone_id, now, mode)
    except ValueError as exc:
        _paper_error(exc)
    await live.hub.broadcast({"type": "paper", "event": "order placed"})
    return row(order)


@app.delete("/api/paper/orders/{order_id}", tags=["paper"])
def paper_cancel(order_id: int, db: Session = Depends(get_db)):
    try:
        return row(paper.cancel(db, order_id))
    except ValueError as exc:
        _paper_error(exc)


@app.get("/api/paper/positions", tags=["paper"])
def paper_positions(db: Session = Depends(get_db)):
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(Instrument.segment.in_(("NSE_EQ", "NSE_INDEX")))).all())
    pending = [{**row(o), "symbol": names.get(o.instrument_key) or o.instrument_key.split("|")[-1],
                "price": live.state["prices"].get(o.instrument_key)} for o in paper.live_orders(db)]
    closed = db.scalars(select(PaperTrade).where(PaperTrade.exit_ts.isnot(None), PaperTrade.archived.isnot(True))
                        .order_by(PaperTrade.exit_ts.desc()).limit(200)).all()
    return {"pending": pending, "open": [_trade_view(t, names) for t in paper.open_trades(db)],
            "closed": [_trade_view(t, names) for t in closed]}


@app.patch("/api/paper/positions/{trade_id}", tags=["paper"])
def paper_edit(trade_id: int, stop: float | None = Body(None), target: float | None = Body(None), db: Session = Depends(get_db)):
    try:
        return row(paper.edit(db, trade_id, stop, target, data_sync.now_ist()))
    except ValueError as exc:
        _paper_error(exc)


@app.post("/api/paper/positions/{trade_id}/close", tags=["paper"])
async def paper_close(trade_id: int, db: Session = Depends(get_db)):
    """Close at the live price, less the slippage setting. The price is fetched from Upstox now if the
    poller has none; without any live price the position cannot be closed."""
    t = db.get(PaperTrade, trade_id)
    if not t or t.exit_ts is not None:
        raise HTTPException(400, "No open position with that id.")
    price = live.state["prices"].get(t.instrument_key) if live.market_open(data_sync.now_ist()) else None
    if price is None:
        price = (await client.ltp([t.instrument_key])).get(t.instrument_key)
    if price is None:
        raise HTTPException(400, "No live price is available for this instrument right now.")
    return row(paper.manual_close(db, trade_id, price, data_sync.now_ist()))


@app.get("/api/paper/summary", tags=["paper"])
def paper_summary(db: Session = Depends(get_db)):
    return paper.summary(db, live.state["prices"])


@app.post("/api/paper/reset", tags=["paper"])
def paper_reset(db: Session = Depends(get_db)):
    """Archive every paper order and trade and start again from the starting capital."""
    return {"archived": paper.reset(db)}


# ---------------------------------------------------------------- options (2)
@app.get("/api/options/{symbol:path}/expiries", tags=["options"])
def option_expiries(symbol: str, db: Session = Depends(get_db)):
    return options.expiries(db, resolve(db, symbol), data_sync.now_ist().date())


def _indication(db: Session, key: str, mode: str, view: dict, expiry: str, iv: dict, now: datetime) -> dict:
    """What the scanner says for this instrument and mode, turned into an option decision: call or put side,
    whether it is time to enter, the plan's levels as estimated premiums, and the confirmation score."""
    saved = db.get(ModeResult, (key, mode))
    r = dict(saved.data) if saved and saved.data else results.build(db, key, mode)
    sig, z = r["signal"], r.get("ltf_zone")
    out = {"mode": mode, "signal": sig["signal"], "reason": sig.get("reason"), "side": None, "curve": r.get("curve"), "itf_trend": r.get("itf_trend"),
           "zone": z, "scanner_score": sig.get("score"), "zone_type": sig.get("zone_type"), "status": None, "status_note": None,
           "entry_type": None, "entry_waiting": False,
           "levels": None, "confirmation": r.get("confirmation")}
    if sig["signal"] == "NO_TRADE" or not z:
        return out
    buy = sig["signal"] == "BUY"
    out["side"] = "call" if buy else "put"
    kind = sig.get("primary_entry") or "TYPE_1"
    plan_ = sig.get({"TYPE_1": "type1", "TYPE_2": "type2", "TYPE_3": "type3"}[kind]) or {}
    stop = plan_.get("stoploss")
    entry = plan_.get("entry") if plan_.get("entry") is not None else z["proximal"]     # types 2 and 3 have no price until they happen
    target = plan_.get("target")
    if target is None and stop is not None:
        target = entry + (1 if buy else -1) * float(settings_store.get(db, "target_rr")) * abs(entry - stop)
    out["entry_type"], out["entry_waiting"] = kind, bool(sig.get("waiting"))
    spot = view.get("spot") or live.state["prices"].get(key) or r.get("cmp")
    if spot and entry and stop:
        away = 100 * ((spot - entry) if buy else (entry - spot)) / entry
        reach = float(settings_store.get(db, "approach_pct"))
        beyond = spot < stop if buy else spot > stop
        out["status"], out["status_note"] = (("invalid", "price is beyond the stop loss") if beyond else
                                             ("enter", "price is at the entry zone") if away <= 0 and kind == "TYPE_1" else
                                             ("enter", f"entry type {kind[-1]} has happened") if away <= 0 and not sig.get("waiting") else
                                             ("get ready", f"price is at the zone: entry type {kind[-1]} waits for the candles to confirm") if away <= 0 else
                                             ("get ready", f"{away:.2f}% from the entry zone") if away <= reach else
                                             ("wait", f"{away:.1f}% from the entry zone"))
    leg = view.get("suggested_leg")
    if leg and spot and entry and stop and target:
        at = {k: confirm.premium_at(leg, spot, v) for k, v in (("entry", entry), ("stop", stop), ("target", target))}
        lot = db.scalar(select(Instrument.lot_size).where(Instrument.instrument_key == leg["instrument_key"])) or 1
        if None not in at.values():
            risk, reward = at["entry"] - at["stop"], at["target"] - at["entry"]
            budget = float(settings_store.get(db, "paper_capital")) * float(settings_store.get(db, "paper_risk_pct")) / 100
            out["levels"] = {"entry": entry, "stop": stop, "target": target, "premium_entry": at["entry"], "premium_stop": at["stop"],
                             "premium_target": at["target"], "lot_size": lot, "risk_per_lot": round(risk * lot, 2), "reward_per_lot": round(reward * lot, 2),
                             "rr": round(reward / risk, 2) if risk > 0 else None, "lots_for_risk": int(budget // (risk * lot)) if risk > 0 else None,
                             "risk_budget": round(budget)}
    days_left = (date.fromisoformat(expiry) - now.date()).days if expiry else None
    extra = confirm.option_checks(sig["signal"], leg, out["levels"], view, iv, days_left, entry, target)
    out["confirmation"] = confirm.summary(((r.get("confirmation") or {}).get("checks") or []) + extra)
    return out


@app.get("/api/options/{symbol:path}", tags=["options"])
async def option_chain(symbol: str, expiry: str | None = None, zone_id: int | None = None, mode: str = "MONTHLY",
                       db: Session = Depends(get_db)):
    """Live option chain near the money, with put-call ratio, open interest change and build-up since the previous close,
    liquidity flags, IV percentile from saved snapshots, the open-interest walls and a suggested strike. `indication`
    carries the scanner's side for the chosen mode, the entry status, premium estimates and the confirmation score."""
    key = resolve(db, symbol)
    now = data_sync.now_ist()
    mode = mode.upper() if mode.upper() in config.MODES else "MONTHLY"
    if not expiry:
        exp = options.expiries(db, key, now.date())
        if not exp:
            raise HTTPException(404, "No option contracts found for this instrument. Refresh instruments, or it has no options.")
        expiry = exp[0]
    zone = db.get(Zone, zone_id) if zone_id else None
    if zone is None:          # no zone named: use the entry zone of the scanner's result for this mode
        saved = db.get(ModeResult, (key, mode))
        z = (saved.data or {}).get("ltf_zone") if saved else None
        if z and (saved.data or {}).get("signal", {}).get("signal") != "NO_TRADE":
            zone = SimpleNamespace(id=z["zone_id"], side=z["side"], proximal=z["proximal"], distal=z["distal"], timeframe=z["timeframe"])
    view = options.build_view(db, await client.option_chain(key, expiry), zone)
    iv = options.iv_percentile(db, key, now)
    return {"instrument_key": key, "expiry": expiry, "as_of": now, **view, "iv": iv,
            "indication": _indication(db, key, mode, view, expiry, iv, now),
            "zone": {"id": zone.id, "side": zone.side, "proximal": zone.proximal, "distal": zone.distal, "timeframe": zone.timeframe} if zone else None}


# ---------------------------------------------------------------- results (2)
def _result_row(db: Session, key: str, mode: str, names: dict, fresh: bool = False) -> dict:
    saved = db.get(ModeResult, (key, mode))
    data = dict(saved.data) if saved and saved.data and not fresh else results.build(db, key, mode)
    price = live.state["prices"].get(key)
    return {**data, "symbol": names.get(key) or key.split("|")[-1], "is_index": "_INDEX|" in key, "live_price": price,
            "updated_at": saved.updated_at if saved else None}


@app.get("/api/results", tags=["scan"])
def results_list(mode: str = "MONTHLY", db: Session = Depends(get_db)):
    """The dashboard: one full GTF result per ready instrument for the chosen mode, plus the instruments still on their way."""
    mode = mode.upper()
    if mode not in config.MODES:
        raise HTTPException(400, f"mode must be one of {list(config.MODES)}")
    keys = universe(db)
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(Instrument.instrument_key.in_(keys))).all())
    state = status.overview(db, keys)
    ready = [k for k in keys if state[k]["ready"]]
    return {"mode": mode, "frames": list(config.MODES[mode]), "total": len(keys), "ready": len(ready),
            "direction_rule": settings_store.get(db, "direction_rule") or "location",
            "approach_pct": settings_store.get(db, "approach_pct"),
            "pending": [{"instrument_key": k, "symbol": names.get(k) or k.split("|")[-1], **state[k]} for k in keys if not state[k]["ready"]],
            "busy": [names.get(k) or k.split("|")[-1] for k in ready if state[k]["state"] in ("downloading", "scanning")],
            "rows": [_result_row(db, k, mode, names) for k in ready]}


@app.get("/api/result/{symbol:path}", tags=["scan"])
def result_one(symbol: str, mode: str = "MONTHLY", db: Session = Depends(get_db)):
    """One instrument in one mode, for the chart."""
    mode = mode.upper()
    if mode not in config.MODES:
        raise HTTPException(400, f"mode must be one of {list(config.MODES)}")
    key = resolve(db, symbol)
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(Instrument.instrument_key == key)).all())
    return _result_row(db, key, mode, names)


# ---------------------------------------------------------------- scan-date check (1)
@app.post("/api/scancheck", tags=["backtest"])
async def scan_check(symbols: list[str] | str = Body("all"), mode: str = Body("MONTHLY"), scan_date: date = Body(...),
                     cost_pct: float = Body(0.05), save: bool = Body(False), db: Session = Depends(get_db)):
    """What the rules said about each stock on one date, from the candles up to that date only, and what then
    happened to the entry its trade score takes (type 1, or types 2 and 3) in the candles after it.
    save=true keeps the report so it can be reopened later from the list of past checks."""
    mode = mode.upper()
    if mode not in config.MODES:
        raise HTTPException(400, f"mode must be one of {list(config.MODES)}")
    keys = status.ready_keys(db, universe(db)) if symbols in (None, "all") else [resolve(db, x) for x in symbols]
    if not keys:
        raise HTTPException(400, "No ready stock to check. Download and scan some first.")
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(Instrument.instrument_key.in_(keys))).all())

    def work() -> list[dict]:
        with SessionLocal() as own:        # in a worker thread, so the pages keep answering
            return [{**scancheck.check(own, k, mode, scan_date, cost_pct), "symbol": names.get(k) or k.split("|")[-1]} for k in keys]
    rows = await asyncio.to_thread(work)
    out = {"id": None, "mode": mode, "scan_date": str(scan_date), "rows": rows, "summary": scancheck.summarize(rows)}
    if save:
        run = BacktestRun(status="done", created_at=data_sync.now_ist(), ended_at=data_sync.now_ist(), progress=f"{len(rows)} stocks",
                          settings={"kind": "scan", "mode": mode, "scan_date": str(scan_date), "symbols": keys, "cost_pct": cost_pct},
                          summary={"summary": out["summary"], "rows": rows})
        db.add(run)
        db.commit()
        out["id"] = run.id
    return out


def _is_scan(r: BacktestRun) -> bool:
    return isinstance(r.settings, dict) and r.settings.get("kind") == "scan"


@app.get("/api/scanchecks", tags=["backtest"])
def scan_checks(limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    """Past scan-date checks, newest first, with their totals per plan."""
    runs = [r for r in db.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(limit * 3)).all() if _is_scan(r)][:limit]
    return [{"id": r.id, "created_at": r.created_at, "mode": r.settings["mode"], "scan_date": r.settings["scan_date"],
             "stocks": len(r.settings.get("symbols") or []), "summary": (r.summary or {}).get("summary")} for r in runs]


@app.get("/api/scancheck/{check_id}", tags=["backtest"])
def scan_check_get(check_id: int, db: Session = Depends(get_db)):
    """One saved scan-date check, exactly as it was reported."""
    r = db.get(BacktestRun, check_id)
    if not r or not _is_scan(r):
        raise HTTPException(404, "No such saved check")
    return {"id": r.id, "created_at": r.created_at, "mode": r.settings["mode"], "scan_date": r.settings["scan_date"],
            "cost_pct": r.settings.get("cost_pct"), "rows": (r.summary or {}).get("rows", []), "summary": (r.summary or {}).get("summary", {})}


@app.delete("/api/scancheck/{check_id}", tags=["backtest"])
def scan_check_delete(check_id: int, db: Session = Depends(get_db)):
    r = db.get(BacktestRun, check_id)
    if not r or not _is_scan(r):
        raise HTTPException(404, "No such saved check")
    db.delete(r)
    db.commit()
    return {"deleted": check_id}