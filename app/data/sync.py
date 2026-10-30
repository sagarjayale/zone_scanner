"""The one place that fetches candles from Upstox. Three callers use it: the cron job,
the sync API and the scan of a single stock. It fetches only what is missing."""
import asyncio
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import DERIVED, FETCHED, SESSION_CLOSE, SESSION_OPEN, TF_MINUTES
from ..models import Candle, Rejection, SyncState, TradePlan, Zone, ZoneEvent
from .resample import resample_daily, resample_intraday

_locks: dict[str, asyncio.Lock] = {}
COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]
SPLIT_TOLERANCE = 0.005  # a re-fetched daily close differing by more than 0.5% means adjusted history


def now_ist() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def candle_end(ts: datetime, tf: str) -> datetime:
    close = datetime.combine(ts.date(), SESSION_CLOSE)
    if tf == "1d":
        return close
    return min(ts + timedelta(minutes=TF_MINUTES[tf]), close)


def upsert_candles(db: Session, key: str, tf: str, rows: list[dict]) -> int:
    """Insert candles, replacing any that already exist. Works on MySQL, PostgreSQL and SQLite."""
    if not rows:
        return 0
    dialect = db.bind.dialect.name
    payload = [{"instrument_key": key, "timeframe": tf, **{c: r[c] for c in COLS}} for r in rows]
    values = [c for c in COLS if c != "ts"]
    for i in range(0, len(payload), 1000):
        chunk = payload[i:i + 1000]
        if dialect == "mysql":
            from sqlalchemy.dialects.mysql import insert
            stmt = insert(Candle).values(chunk)
            stmt = stmt.on_duplicate_key_update(**{c: stmt.inserted[c] for c in values})
        else:
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            stmt = insert(Candle).values(chunk)
            stmt = stmt.on_conflict_do_update(index_elements=["instrument_key", "timeframe", "ts"],
                                              set_={c: getattr(stmt.excluded, c) for c in values})
        db.execute(stmt)
        db.commit()      # one short write per batch, so nothing else waits long for the database
    return len(payload)


def load_candles(db: Session, key: str, tf: str, since: datetime | None = None,
                 until: datetime | None = None, last: int | None = None) -> pd.DataFrame:
    q = select(*[getattr(Candle, c) for c in COLS]).where(Candle.instrument_key == key, Candle.timeframe == tf)
    if since is not None:
        q = q.where(Candle.ts >= since)
    if until is not None:
        q = q.where(Candle.ts <= until)
    if last:
        rows = db.execute(q.order_by(Candle.ts.desc()).limit(last)).all()[::-1]
    else:
        rows = db.execute(q.order_by(Candle.ts)).all()
    df = pd.DataFrame(rows, columns=COLS)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


def get_state(db: Session, key: str, tf: str) -> SyncState:
    st = db.get(SyncState, (key, tf))
    if st is None:
        st = SyncState(instrument_key=key, timeframe=tf)
        db.add(st)
        db.flush()
    return st


def _rebuild_derived(db: Session, key: str, src_tf: str, since: date, now: datetime) -> None:
    fiscal = bool(settings_store.get(db, "fiscal_year"))
    for dtf, src in DERIVED.items():
        if src != src_tf:
            continue
        if src_tf in ("5m", "30m"):
            start = datetime.combine(since, SESSION_OPEN)
            out = resample_intraday(load_candles(db, key, src_tf, since=start), TF_MINUTES[dtf], now)
        else:
            start = datetime(since.year - 2, 1, 1)  # wide enough for any year or financial year in progress
            out = resample_daily(load_candles(db, key, "1d", since=start), dtf, now, fiscal)
            earlier = db.scalar(select(func.count()).where(Candle.instrument_key == key, Candle.timeframe == "1d", Candle.ts < start))
            if earlier:  # the first period is cut by the window, so keep the stored one
                out = out.iloc[1:]
        upsert_candles(db, key, dtf, out.to_dict("records"))
        st = get_state(db, key, dtf)
        st.last_candle_ts = db.scalar(select(func.max(Candle.ts)).where(Candle.instrument_key == key, Candle.timeframe == dtf))
        st.last_sync_at = now


def reset_symbol(db: Session, key: str) -> None:
    """Remove everything stored for one instrument. Used when Upstox has adjusted its price
    history (split or bonus): old candles and the zones built on them no longer match."""
    zone_ids = select(Zone.id).where(Zone.instrument_key == key)
    db.execute(delete(ZoneEvent).where(ZoneEvent.zone_id.in_(zone_ids)))
    db.execute(delete(TradePlan).where(TradePlan.zone_id.in_(zone_ids)))
    for model in (Zone, Candle, SyncState, Rejection):
        db.execute(delete(model).where(model.instrument_key == key))
    db.commit()


def _store(db: Session, key: str, tf: str, cfg: dict, rows: list[dict], first_time: bool, start: date, now: datetime) -> int | None:
    """Write one fetched timeframe: candles, marker, gap count and the timeframes built from it.
    Returns the number of candles written, or None when the stored daily history no longer matches Upstox."""
    st = get_state(db, key, tf)
    if tf == "1d" and not first_time:
        stored = db.get(Candle, (key, "1d", st.last_candle_ts))
        again = next((r for r in rows if r["ts"] == st.last_candle_ts), None)
        if stored and again and stored.close and abs(again["close"] - stored.close) / stored.close > SPLIT_TOLERANCE:
            return None
    written = upsert_candles(db, key, tf, rows)
    if cfg.get("keep_days"):
        cutoff = datetime.combine(now.date() - timedelta(days=cfg["keep_days"]), SESSION_OPEN)
        db.execute(delete(Candle).where(Candle.instrument_key == key, Candle.timeframe == tf, Candle.ts < cutoff))
    st.last_candle_ts = db.scalar(select(func.max(Candle.ts)).where(Candle.instrument_key == key, Candle.timeframe == tf))
    st.last_sync_at = now
    st.gaps = gap_count(db, key, tf, now)
    _rebuild_derived(db, key, tf, start, now)
    db.commit()
    return written


async def _sync_once(db: Session, client, key: str, wanted: set, full: bool, repair_days: int, now: datetime):
    """One pass over the fetched timeframes. Returns (written, history_changed)."""
    result: dict[str, int] = {}
    for tf, cfg in FETCHED.items():
        if tf not in wanted:
            continue
        st = get_state(db, key, tf)
        db.commit()      # never hold a write open across a network wait: other instruments are syncing alongside
        today = now.date()
        first_time = st.last_candle_ts is None or full
        if first_time:
            start = cfg["start"] or today - timedelta(days=cfg["keep_days"])
        elif repair_days and tf != "1d":
            start = today - timedelta(days=repair_days)
        else:
            start = st.last_candle_ts.date()  # same day again: a small overlap, removed by the upsert
        live_day = tf != "1d" and today.weekday() < 5 and now.time() >= SESSION_OPEN
        # Marker already on today: the intraday call covers everything missing, so skip the history call.
        only_intraday = live_day and not first_time and not repair_days and st.last_candle_ts.date() == today
        rows, cur = [], start
        while cur <= today and not only_intraday:
            chunk_end = min(cur + timedelta(days=cfg["chunk_days"] - 1), today)
            rows += await client.historical(key, cfg["unit"], cfg["interval"], chunk_end, cur)
            cur = chunk_end + timedelta(days=1)
        if live_day:
            rows += await client.intraday(key, cfg["unit"], cfg["interval"])
        rows = [r for r in rows if candle_end(r["ts"], tf) <= now]  # never store a forming candle
        # Storing and resampling is plain database and pandas work. It runs in a worker thread, so the web pages
        # keep answering while a long download is written.
        written = await asyncio.to_thread(_store, db, key, tf, cfg, rows, first_time, start, now)
        if written is None:
            return result, True
        result[tf] = written
    return result, False


async def sync_symbol(db: Session, client, key: str, timeframes: list[str] | None = None, full: bool = False,
                      repair_days: int = 0, now: datetime | None = None) -> dict:
    """Incremental sync for one instrument. Returns candles written per fetched timeframe.
    If the stored daily close no longer matches Upstox, the history was adjusted: everything
    for this instrument is cleared and downloaded again, and `history_reset` is set."""
    now = now or now_ist()
    wanted = {DERIVED.get(tf, tf) for tf in timeframes} if timeframes else set(FETCHED)
    async with _locks.setdefault(key, asyncio.Lock()):
        result, changed = await _sync_once(db, client, key, wanted, full, repair_days, now)
        if changed:
            db.rollback()
            reset_symbol(db, key)
            result, _ = await _sync_once(db, client, key, set(FETCHED), True, 0, now)
            result["history_reset"] = True
    return result


def gap_count(db: Session, key: str, tf: str, now: datetime, days: int = 30) -> int:
    """Number of recent sessions with fewer intraday candles than a full session has."""
    expected = {"5m": 75, "30m": 13}.get(tf)
    if not expected:
        return 0
    since = datetime.combine(now.date() - timedelta(days=days), SESSION_OPEN)
    day = func.date(Candle.ts)
    rows = db.execute(select(day, func.count()).where(Candle.instrument_key == key, Candle.timeframe == tf,
                                                      Candle.ts >= since).group_by(day)).all()
    today = str(now.date())
    return sum(1 for d, n in rows if n < expected and str(d) != today)


def needs_sync(db: Session, key: str, now: datetime | None = None) -> bool:
    """True if a 5-minute bar has closed since the last stored one (or nothing is stored)."""
    now = now or now_ist()
    st = db.get(SyncState, (key, "5m"))
    if st is None or st.last_candle_ts is None:
        return True
    session_now = min(now, datetime.combine(now.date(), SESSION_CLOSE))
    return session_now - candle_end(st.last_candle_ts, "5m") >= timedelta(minutes=5)
