"""Sync and scan against a fake Upstox client. The candles are generated in the test only;
the application itself never contains made-up data."""
import asyncio
from datetime import date, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import func, select

from app.data import sync as data_sync
from app.db import Base, SessionLocal, engine
from app.engine import scanner
from app.models import Candle, SyncState, TradePlan, Zone

KEY = "NSE_EQ|TEST"
NOW = datetime(2026, 10, 2, 16, 0)   # a Friday, after the close


class FakeClient:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _bars(day: date, minutes: int):
        if day.weekday() >= 5:
            return []
        rng = np.random.default_rng(day.toordinal())
        level = 500 + 80 * np.sin(day.toordinal() / 40) + rng.normal(0, 3)
        if minutes == 0:
            starts = [datetime.combine(day, datetime.min.time())]
        else:
            starts = [datetime(day.year, day.month, day.day, 9, 15) + timedelta(minutes=minutes * i)
                      for i in range(-(-375 // minutes))]
        out, px = [], level
        for ts in starts:
            move = rng.normal(0, 4 if minutes == 0 else 1.5) * (3 if rng.random() < 0.12 else 1)
            o, c = px, px + move
            hi, lo = max(o, c) + abs(rng.normal(0, 0.6)), min(o, c) - abs(rng.normal(0, 0.6))
            out.append({"ts": ts, "open": o, "high": hi, "low": lo, "close": c, "volume": 1000, "oi": 0})
            px = c
        return out

    async def historical(self, key, unit, interval, to_date, from_date):
        self.calls.append((unit, interval, from_date, to_date))
        minutes = 0 if unit == "days" else interval
        out, d = [], from_date
        while d <= to_date:
            out += self._bars(d, minutes)
            d += timedelta(days=1)
        return out

    async def intraday(self, key, unit, interval):
        return []


@pytest.fixture(scope="module")
def db():
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s


def count(db, tf):
    return db.scalar(select(func.count()).where(Candle.instrument_key == KEY, Candle.timeframe == tf))


def test_first_sync_fills_all_twelve_timeframes(db):
    client = FakeClient()
    asyncio.run(data_sync.sync_symbol(db, client, KEY, now=NOW))
    for tf in ("5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1mo", "3mo", "6mo", "1y"):
        assert count(db, tf) > 0, tf
    assert count(db, "30m") // 13 == count(db, "1h") // 7 == count(db, "4h") // 2 == count(db, "2h") // 4
    assert count(db, "5m") // 75 == count(db, "15m") // 25
    assert db.get(SyncState, (KEY, "5m")).last_candle_ts == datetime(2026, 10, 2, 15, 25)
    assert db.get(SyncState, (KEY, "5m")).gaps == 0


def test_second_sync_fetches_only_from_marker_and_changes_nothing(db):
    before = {tf: count(db, tf) for tf in ("5m", "30m", "1d", "1w", "1y")}
    client = FakeClient()
    asyncio.run(data_sync.sync_symbol(db, client, KEY, now=NOW))
    assert all(frm == date(2026, 10, 2) for _, _, frm, _ in client.calls)   # only the marker day is requested
    assert before == {tf: count(db, tf) for tf in before}


def test_forming_candles_are_never_stored(db):
    client = FakeClient()
    mid = datetime(2026, 10, 5, 10, 32)   # Monday, mid-session
    asyncio.run(data_sync.sync_symbol(db, client, KEY, now=mid))
    last5 = db.get(SyncState, (KEY, "5m")).last_candle_ts
    assert last5 == datetime(2026, 10, 5, 10, 25)
    assert db.get(SyncState, (KEY, "30m")).last_candle_ts == datetime(2026, 10, 5, 9, 45)
    assert db.get(SyncState, (KEY, "1d")).last_candle_ts == datetime(2026, 10, 2)
    assert not data_sync.needs_sync(db, KEY, now=mid)
    assert data_sync.needs_sync(db, KEY, now=mid + timedelta(minutes=5))


def test_scan_creates_zones_and_is_incremental(db):
    first = scanner.scan_symbol(db, KEY, now=NOW)
    assert sum(s["zones"] for s in first) > 0
    zones = db.scalars(select(Zone).where(Zone.instrument_key == KEY)).all()
    assert {z.status for z in zones} <= {"fresh", "tested", "violated", "consumed"}
    for z in zones:
        assert (z.proximal > z.distal) == (z.side == "demand")
        assert z.legin_explosive is True and z.distal_from in ("base", "leg-in", "leg-out") and z.legout_count in (1, 2)
        assert 0.5 < z.legout_body <= 1.0 or z.has_gap                                     # a gap candle may have a small body
        assert (z.touches == 0) == (z.status == "fresh") or z.status == "violated"       # fresh means never tested
        if z.status == "consumed":
            assert z.touches == 2 and not z.in_visit                                       # tested twice, and that test is over
    active = [z for z in zones if z.status in ("fresh", "tested")]
    assert all(z.label in ("TYPE_1", "TYPE_2_3", "WEAK", "REJECT") and 0 <= z.score <= 8 for z in active)
    assert all(z.touches <= 2 for z in active) and any(z.legout_count == 2 for z in zones) and any(z.t3_entry is not None for z in zones)
    assert {z.distal_from for z in zones} == {"base", "leg-in", "leg-out"}                 # all three markings of the distal line occur
    assert all(z.distal_from != "leg-in" for z in zones if z.pattern in ("RBR", "DBD"))    # the leg-in wick is for reversal patterns only
    plans = db.scalars(select(TradePlan).where(TradePlan.is_current.is_(True))).all()
    for p in plans:        # every plan belongs to a mode and waits for the entry its trade score takes
        z = db.get(Zone, p.zone_id)
        score = p.context["score"]
        assert p.mode and p.stop == z.distal and p.reward_risk == 2.0 and score >= 5 and p.context["side"] in ("BUY", "SELL")
        assert p.entry_type == ("TYPE_1" if score >= 7 else "TYPE_3") and set(p.plans) == {"type1", "type2", "type3"}
        assert abs(abs(p.target - p.entry) - 2 * abs(p.entry - p.stop)) < 1e-6
        if p.status == "pending":
            assert p.entry == z.proximal and p.triggered_ts is None                        # until the entry happens, the proximal line stands in
        if p.status == "triggered" and p.entry_type == "TYPE_3":
            assert (p.entry > z.proximal) == (z.side == "demand") and p.triggered_ts is not None
    n_zones = len(zones)
    second = scanner.scan_symbol(db, KEY, now=NOW)
    assert sum(s["candles"] for s in second) == 0
    assert db.scalar(select(func.count()).select_from(Zone)) == n_zones
    by_tf = {}
    for z in zones:
        by_tf[z.timeframe] = by_tf.get(z.timeframe, 0) + 1
    print("zones per timeframe:", by_tf, "active:", len(active), "plans:", len(plans), "modes:", sorted({p.mode for p in plans}))
