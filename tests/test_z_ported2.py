"""Second set carried over from the crypto scanner: minimum stop %, reaction zones (GTF PDF p.35-36), the next HTF
check (p.30), the ITF trend by average move, "signal since", the chart in one request, the forming candle, and the
zone pattern on the pages."""
from datetime import datetime, timedelta

import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import live, settings_store
from app.config import DEFAULTS
from app.data import forming
from app.data.sync import upsert_candles
from app.db import SessionLocal
from app.engine import gtf, result
from app.main import app
from app.models import ModeResult, Setting, Zone
from tests.test_z_other_ltf_zones import T0, build, seed, zone


def _unset(db, *keys):
    for k in keys:
        row = db.get(Setting, k)
        if row is not None:
            db.delete(row)
    db.commit()


def test_new_filters_are_off_by_default():
    assert DEFAULTS["min_stop_pct"] == 0 and DEFAULTS["skip_reaction_zones"] is False and DEFAULTS["check_next_htf"] is False
    assert DEFAULTS["itf_rule"] == "pct" and DEFAULTS["itf_slope_pct"] == 0.05


def test_min_stop_pct_skips_thin_zones_and_says_why():
    with SessionLocal() as db:
        a, b, c = seed(db, "NSE_EQ|THIN1")                 # A 521-514 (1.3%), B 511-506 (1.0%), C 504-500 (0.8%)
        settings_store.put(db, "min_stop_pct", 1.2)
        try:
            r = build(db, "NSE_EQ|THIN1")
            assert r["ltf_zone"]["proximal"] == 521.0 and r["other_ltf_zones"] == []
            assert {s["proximal"] for s in r["skipped_ltf_zones"]} == {511.0, 504.0}
            assert all("too thin" in s["why"] for s in r["skipped_ltf_zones"])
            settings_store.put(db, "min_stop_pct", 2.0)
            r = build(db, "NSE_EQ|THIN1")
            assert r["signal"]["signal"] == "NO_TRADE" and r["signal"]["reason"].startswith("The LTF zone is not traded: zone too thin")
        finally:
            _unset(db, "min_stop_pct")


def test_reaction_zone_and_next_htf_rules():
    th = dict(DEFAULTS)
    early = zone("K", "30m", "demand", 520.0, 510.0, "DBR", legout_count=1)
    late = zone("K", "30m", "demand", 515.0, 505.0, "DBR", legout_count=1)
    early.created_ts, late.created_ts = T0, T0 + timedelta(hours=2)
    late.legout_body, late.has_gap = 0.55, False                  # a weak leg-out
    assert gtf.reaction_of(late, [early, late], th) is early     # its base sits on the earlier zone
    assert gtf.reaction_of(early, [early, late], th) is None
    strong = zone("K", "30m", "demand", 515.0, 505.0, "DBR", legout_count=2)
    strong.created_ts = T0 + timedelta(hours=2)
    assert gtf.reaction_of(strong, [early, strong], th) is None  # a strong leg-out is tradeable all the same
    # entry 520, stop 510, target 540: a supply at 530-535 one timeframe up is in the way, one at 545-550 is not
    z = zone("K", "30m", "demand", 520.0, 510.0, "DBR")
    assert gtf.blocked_above(z, [zone("K", "1w", "supply", 530.0, 535.0, "RBD")], th) is not None
    assert gtf.blocked_above(z, [zone("K", "1w", "supply", 545.0, 550.0, "RBD")], th) is None
    assert gtf.next_htf("DAILY") == "1w" and gtf.next_htf("4HR") == "1d" and gtf.next_htf("YEARLY") is None
    with SessionLocal() as db:
        seed(db, "NSE_EQ|NEXT1")                                 # DAILY: entry A at 521, stop 514, target 535
        db.add(zone("NSE_EQ|NEXT1", "1w", "supply", 528.0, 533.0, "RBD"))
        db.commit()
        assert build(db, "NSE_EQ|NEXT1")["ltf_zone"]["proximal"] == 521.0      # off: traded
        settings_store.put(db, "check_next_htf", True)
        try:
            r = build(db, "NSE_EQ|NEXT1")
            assert r["ltf_zone"] is None or r["ltf_zone"]["proximal"] != 521.0
            assert any("1w supply zone" in s["why"] for s in r["skipped_ltf_zones"])
        finally:
            _unset(db, "check_next_htf")


def test_itf_trend_by_average_move():
    rng = np.random.default_rng(3)
    quiet = 100 + np.cumsum(rng.normal(0.02, 0.05, 80))         # a slow steady rise
    pct = gtf.itf_trend(quiet, {"itf_rule": "pct", "itf_slope_pct": 0.05})
    assert pct == gtf.itf_trend(quiet)                            # the default is scanner.py's 0.05% of price
    atr = gtf.itf_trend(quiet, {"itf_rule": "atr", "itf_atr_mult": 0.1})
    assert atr["trend"] == "UP" and "ATR rule" in atr["reason"]
    i = np.arange(80)
    swings = 1000 + 0.0857 * i + np.where(i % 2 == 0, 20, -20)  # a small rise inside large swings of 40 a candle
    assert gtf.itf_trend(swings, {"itf_rule": "pct"})["trend"] == "UP"              # 0.6 beats 0.05% of 1000
    assert gtf.itf_trend(swings, {"itf_rule": "atr", "itf_atr_mult": 0.1})["trend"] == "SIDEWAYS"   # but not 0.1 x 40
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"itf_rule": "magic"}).status_code == 400
        assert c.put("/api/settings", json={"min_stop_pct": 50}).status_code == 400
        assert c.put("/api/settings", json={"check_next_htf": "yes"}).status_code == 400


def test_signal_since_moves_only_when_the_signal_changes():
    with SessionLocal() as db:
        seed(db, "NSE_EQ|SINCE1")
        r = build(db, "NSE_EQ|SINCE1")
        t1, t2, t3 = datetime(2026, 6, 2, 10, 0), datetime(2026, 6, 2, 10, 5), datetime(2026, 6, 2, 10, 10)
        result.save(db, "NSE_EQ|SINCE1", "DAILY", r, t1)
        result.save(db, "NSE_EQ|SINCE1", "DAILY", r, t2)                 # same signal: the time stays
        db.commit()
        assert db.get(ModeResult, ("NSE_EQ|SINCE1", "DAILY")).signal_since == t1
        r2 = {**r, "signal": {**r["signal"], "signal": "NO_TRADE"}}
        result.save(db, "NSE_EQ|SINCE1", "DAILY", r2, t3)
        db.commit()
        assert db.get(ModeResult, ("NSE_EQ|SINCE1", "DAILY")).signal_since == t3
    with TestClient(app) as c:
        assert c.get("/api/result/NSE_EQ|SINCE1?mode=DAILY").json()["signal_since"].startswith("2026-06-02T10:10")


def test_chart_in_one_request_and_patterns():
    with SessionLocal() as db:
        seed(db, "NSE_EQ|CHART1")
    with TestClient(app) as c:
        d = c.get("/api/chart/NSE_EQ|CHART1?tf=30m&mode=DAILY&overlays=1d,30m&old=true").json()
        assert len(d["candles"]) == 3 and {z["pattern"] for z in d["zones"]} == {"DBR"} and d["old"] == []
        assert list(d["overlays"]) == ["1d"] and {z["pattern"] for z in d["overlays"]["1d"]} == {"RBR", "DBD"}
        assert d["result"]["demand_zone"]["pattern"] == "RBR" and d["result"]["ltf_zone"]["pattern"] == "DBR"
        assert c.get("/api/chart/NSE_EQ|CHART1?tf=7m").status_code == 400
        page = c.get("/chart").text
        assert 'id="l-pattern" checked' in page and 'id="l-forming" checked' in page
        js = c.get("/static/js/chart.js").text
        assert "/api/chart/" in js and "z.pattern" in js and "/api/forming/" in js


def test_forming_candle_is_built_from_stored_candles_and_the_live_price(monkeypatch):
    key = "NSE_EQ|FORM1"
    day = datetime(2026, 6, 3)
    with SessionLocal() as db:
        upsert_candles(db, key, "5m", [{"ts": day.replace(hour=9, minute=15) + timedelta(minutes=5 * n), "open": 100 + n, "high": 102 + n,
                                        "low": 99 + n, "close": 101 + n, "volume": 1, "oi": 0} for n in range(4)])     # 09:15 to 09:30
        upsert_candles(db, key, "1d", [{"ts": datetime(2026, 6, 1), "open": 90.0, "high": 95.0, "low": 85.0, "close": 92.0, "volume": 1, "oi": 0},
                                       {"ts": datetime(2026, 6, 2), "open": 92.0, "high": 98.0, "low": 91.0, "close": 97.0, "volume": 1, "oi": 0}])
        now = day.replace(hour=9, minute=37)
        c30 = forming.candle(db, key, "30m", 110.0, now)                 # 09:15 to 09:45: the four 5 min candles and the price
        assert c30 == {"ts": "2026-06-03T09:15:00", "open": 100.0, "high": 110.0, "low": 99.0, "close": 110.0, "forming": True}
        c5 = forming.candle(db, key, "5m", None, now)                    # 09:35: no stored candle yet, no price
        assert c5 is None
        wk = forming.candle(db, key, "1w", 96.0, now)                    # Mon 1 Jun to now: two days, today's candles, the price
        assert (wk["ts"], wk["open"], wk["high"], wk["low"], wk["close"]) == ("2026-06-01T00:00:00", 90.0, 105.0, 85.0, 96.0)
        assert forming.candle(db, key, "1d", 96.0, day.replace(hour=16)) is None     # after the close: nothing forms
        assert forming.period_start(day.replace(hour=11, minute=20), "1h") == day.replace(hour=11, minute=15)
    with TestClient(app) as c:
        monkeypatch.setattr(live, "market_open", lambda now: False)
        assert c.get(f"/api/forming/{key}?tf=30m").json() == {"candle": None, "market_open": False}
        monkeypatch.setattr(live, "market_open", lambda now: True)
        monkeypatch.setattr("app.main.data_sync.now_ist", lambda: now)
        monkeypatch.setitem(live.state["prices"], key, 111.0)
        got = c.get(f"/api/forming/{key}?tf=30m").json()["candle"]
        assert got["close"] == 111.0 and got["high"] == 111.0 and got["forming"] is True
    with SessionLocal() as db:                                           # never stored
        from app.models import Candle
        assert not db.scalars(select(Candle).where(Candle.instrument_key == key, Candle.timeframe == "30m")).all()
