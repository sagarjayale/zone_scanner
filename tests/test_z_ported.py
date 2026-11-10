"""Features carried over from the crypto scanner: GTF EYE and Premium pages, minimum and maximum base candles for LTF
zones, the trailing stop loss (paper trading and the scan-date check), CSV downloads, deleting a backtest run, and
the scan workers. (The file name sorts after test_sync_scan, whose candles the scan-date checks use.)"""
import asyncio
import csv
import io
import time
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import live, settings_store
from app.config import DEFAULTS
from app.data.sync import upsert_candles
from app.db import SessionLocal
from app.engine import eye, gtf
from app.jobs import scanpool
from app.main import app
from app.models import EyeRow, BacktestRun, BacktestTrade, InstrumentStatus, PaperOrder, PaperTrade, Setting, Zone
from app.sim import paper, scancheck, trailing
from tests.test_sync_scan import KEY
from tests.test_z_other_ltf_zones import build, seed

STEP = trailing.options({"trail_method": "step"})


def _unset(db, *keys):
    for k in keys:
        row = db.get(Setting, k)
        if row is not None:
            db.delete(row)
    db.commit()


# ---------------------------------------------------------------- trailing stop rules
def test_trailing_off_moves_nothing():
    assert DEFAULTS["trail_method"] == "lock" and DEFAULTS["trail_tf"] == "live" and trailing.is_on(trailing.options(DEFAULTS))
    assert not trailing.is_on(trailing.options({}))                  # a saved check without trailing keys: off
    off = trailing.options({"trail_method": "off"})
    assert not trailing.is_on(off)
    t = trailing.Trailer("demand", 100, 90, off, target=120)
    assert t.close_bar(150, 140, 145) is None and t.stop == 90 and t.live_target() == 120


def test_step_method_long_and_short():
    t = trailing.Trailer("demand", 100, 90, STEP, target=120, cost_frac=0.001)
    assert t.close_bar(105, 99, 104) is None and t.stop == 90           # +0.5 R: not started
    assert t.close_bar(110, 104, 109) == pytest.approx(100.1)            # +1 R: break-even plus charges
    assert t.close_bar(115, 109, 114) == pytest.approx(105)              # +1.5 R: stop at +0.5 R
    assert t.close_bar(112, 100, 101) is None and t.stop == pytest.approx(105)   # never moves back
    assert t.moves == 2 and t.live_target() == 120                       # the target is kept by default
    s = trailing.Trailer("supply", 100, 110, STEP, target=80)
    s.close_bar(101, 85, 86)                                             # best 85: +1.5 R
    assert s.stop == pytest.approx(95)


def test_target_removed_once_trailing_starts_when_asked():
    t = trailing.Trailer("demand", 100, 90, trailing.options({"trail_method": "step", "trail_keep_target": False}), target=120)
    t.close_bar(105, 99, 104)
    assert t.live_target() == 120
    t.close_bar(111, 104, 110)
    assert t.target_removed and t.live_target() == float("inf")


def test_lock_atr_and_swing_methods():
    lock = trailing.Trailer("demand", 100, 90, trailing.options({"trail_method": "lock"}), target=200)
    assert lock.target_removed                                           # "move": only the locked stop ends the trade
    lock.close_bar(111, 100, 110)
    assert lock.stop == 90                                               # 111 is not past 110 + the 2 gap
    lock.close_bar(112, 105, 111)
    assert lock.stop == pytest.approx(110)
    lock.close_bar(205, 150, 200)                                        # the target moves forward one step at a time
    assert lock.target == pytest.approx(210) and lock.stop == pytest.approx(200)   # best 205: 10 steps passed by the gap
    atr = trailing.Trailer("demand", 100, 90, trailing.options({"trail_method": "atr", "trail_atr_mult": 2}),
                           prior=[(101, 99, 100)] * 15, target=120)
    atr.close_bar(112, 110, 111)                                         # +1.2 R; ATR grows with this wide candle
    assert atr.stop >= 100 and atr.stop <= 112
    sw = trailing.Trailer("demand", 100, 90, trailing.options({"trail_method": "swing", "trail_swing": 1, "trail_start_r": 0.25}), target=150)
    for bar in [(112, 108, 111), (113, 104, 112), (118, 110, 117)]:      # the middle low 104 is a swing low
        sw.close_bar(*bar)
    assert 103 < sw.stop < 104


# ---------------------------------------------------------------- min and max base candles
def test_base_candle_limits_choose_the_ltf_zones():
    z = Zone(base_count=5)
    assert gtf.base_count_off(z, {}) is None
    assert "minimum 6" in gtf.base_count_off(z, {"ltf_min_bases": 6})
    assert "maximum 4" in gtf.base_count_off(z, {"ltf_max_bases": 4})
    with SessionLocal() as db:
        a, b, c = seed(db, "NSE_EQ|BASES1")                              # zone C has 5 base candles, A and B 2
        assert [o["zone_id"] for o in build(db, "NSE_EQ|BASES1")["other_ltf_zones"]] == [b, c]
        settings_store.put(db, "ltf_max_bases", 4)
        try:
            r = build(db, "NSE_EQ|BASES1")
            assert r["ltf_zone"]["proximal"] == 521.0 and [o["zone_id"] for o in r["other_ltf_zones"]] == [b]
            settings_store.put(db, "ltf_min_bases", 3)                   # every zone has fewer than 3 base candles now
            assert build(db, "NSE_EQ|BASES1")["signal"]["signal"] == "NO_TRADE"
        finally:
            _unset(db, "ltf_max_bases", "ltf_min_bases")


def test_settings_check_the_new_values():
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"ltf_min_bases": 1.5}).status_code == 400
        assert c.put("/api/settings", json={"ltf_min_bases": 4, "ltf_max_bases": 2}).status_code == 400
        assert c.put("/api/settings", json={"trail_method": "magic"}).status_code == 400
        assert c.put("/api/settings", json={"trail_tf": "1m"}).status_code == 400
        assert c.put("/api/settings", json={"trail_start_r": 0}).status_code == 400
        s = c.put("/api/settings", json={"trail_method": "step", "trail_start_r": 1.5, "trail_keep_target": False}).json()
        assert (s["trail_method"], s["trail_start_r"], s["trail_keep_target"]) == ("step", 1.5, False)
    with SessionLocal() as db:
        _unset(db, "trail_method", "trail_start_r", "trail_keep_target")


# ---------------------------------------------------------------- trailing in the scan-date check
def test_scan_check_with_a_trailing_stop():
    found = 0
    with SessionLocal() as db:
        for month in range(4, 10):
            for day in (3, 10, 17, 24):
                plain = scancheck.check(db, KEY, "HOURLY", date(2026, month, day), 0.0, trailing.options({"trail_method": "off"}))
                if plain["signal"]["signal"] == "NO_TRADE":
                    continue
                same = scancheck.check(db, KEY, "HOURLY", date(2026, month, day), 0.0)    # the settings: off
                assert same["outcomes"] == plain["outcomes"]
                moved = scancheck.check(db, KEY, "HOURLY", date(2026, month, day), 0.0, STEP)
                for name, o in moved["outcomes"].items():
                    p = plain["outcomes"][name]
                    assert o["status"] in ("target hit", "stopped", "trailed out", "open", "never entered", "not taken")
                    if p["status"] in ("never entered", "not taken"):
                        assert o == p                                     # trailing never changes whether a trade is entered
                        continue
                    assert (o["entry"], o["entry_ts"], o["stop"]) == (p["entry"], p["entry_ts"], p["stop"])
                    found += 1
                    if o["status"] == "trailed out":
                        long = moved["ltf_zone"]["side"] == "demand"
                        assert o["stop_moves"] > 0 and o["result_r"] > -0.01
                        assert (o["final_stop"] > o["stop"]) if long else (o["final_stop"] < o["stop"])
                    if o["status"] == "stopped":
                        assert o["stop_moves"] == 0 and p["status"] == "stopped"
                s = scancheck.summarize([moved])
                for v in s.values():
                    assert v["entered"] == v["target_hit"] + v["stopped"] + v["open"]
    assert found, "no entered trade found on any sampled scan date"


# ---------------------------------------------------------------- trailing in paper trading
def test_paper_position_trails_its_stop_and_closes_as_trail():
    key, now = "NSE_EQ|TRAIL1", datetime(2026, 6, 2, 10, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        settings_store.put(db, "trail_method", "step")
        settings_store.put(db, "trail_tf", "ltf")
        try:
            order = paper.place(db, a, now, "DAILY")
            trade = paper.enter_now(db, order.id, 525.0, now)
        finally:
            _unset(db, "trail_method", "trail_tf")
        assert trade.trail["trail_method"] == "step" and trade.trail["tf"] == "30m" and trade.trail["first_stop"] == 514.0
        entry, risk = trade.fill_price, trade.fill_price - 514.0
        # 30 min candles after the fill: up 1.6 R, so the stop goes to +0.5 R
        rows = [{"ts": now + timedelta(minutes=30 * n), "open": entry, "high": entry + risk * r, "low": entry, "close": entry + risk * r * 0.9,
                 "volume": 1, "oi": 0} for n, r in enumerate((0.5, 1.2, 1.6), start=1)]
        upsert_candles(db, key, "30m", rows)
        ev = paper.update_trailing(db, trade, now + timedelta(hours=3))
        assert ev and ev[0]["event"] == "stop moved"
        assert trade.stop == pytest.approx(round(round((entry + 0.5 * risk) / 0.05) * 0.05, 2)) and trade.trail["moves"] >= 1   # on the tick
        assert any(h.get("note", "").startswith("trailing stop") for h in trade.history)
        assert paper.update_trailing(db, trade, now + timedelta(hours=3)) == []      # the same candles: nothing more
        db.commit()
        tid = trade.id
        paper.on_prices(db, {key: trade.stop - 0.5}, now + timedelta(hours=4))
        t = db.get(PaperTrade, tid)
        assert t.exit_reason == "trail" and t.result == "win"
    with TestClient(app) as client:
        r = client.get("/api/paper/export")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        lines = list(csv.DictReader(io.StringIO(r.text)))
        mine = [x for x in lines if x["id"] == str(tid)]
        assert mine and mine[0]["exit_reason"] == "trail" and mine[0]["trail_method"] == "step" and float(mine[0]["first_stop"]) == 514.0
        assert int(mine[0]["stop_moves"]) >= 1
    with SessionLocal() as db:
        for model in (PaperTrade, PaperOrder):
            for row in db.scalars(select(model).where(model.instrument_key == key)):
                db.delete(row)
        db.commit()


# ---------------------------------------------------------------- GTF EYE and Premium
def test_side_view_states():
    def z(side, prox, dist, touches=0, created=datetime(2026, 1, 1)):
        return Zone(side=side, proximal=prox, distal=dist, touches=touches, in_visit=False, status="fresh", score=7.0,
                    created_ts=created, pattern="DBR", label="TYPE_1", base_count=2, legout_count=2, legout_body=0.8, has_gap=False)
    d = z("demand", 100, 95)
    later = datetime(2026, 2, 1)
    assert eye.side_view([d], "demand", 97, [], 1.0)["state"] == "in_zone"
    assert eye.side_view([d], "demand", 100.5, [], 1.0)["state"] == "approaching"
    assert eye.side_view([d], "demand", 110, [], 1.0)["state"] is None
    v = eye.side_view([d], "demand", 104, [(105, 99, 103, later)], 1.0)
    assert v["state"] == "reacting" and v["reaction_r"] == 0.8
    assert eye.side_view([d], "demand", 120, [(105, 99, 103, later)], 1.0)["state"] is None    # too far away now
    assert eye.side_view([z("demand", 100, 95, touches=3)], "demand", 97, [], 1.0, max_tests=1) is None
    assert eye.side_view([], "supply", 100, [], 1.0) is None


def test_eye_api_and_pages():
    with SessionLocal() as db:
        seed(db, "NSE_EQ|EYE1")
        db.merge(InstrumentStatus(instrument_key="NSE_EQ|EYE1", state="ready", ready=True))
        universe = list(settings_store.get(db, "universe") or [])
        settings_store.put(db, "universe", universe + ["NSE_EQ|EYE1"])
    try:
        with TestClient(app) as c:
            assert c.get("/api/eye?mode=MINUTE").status_code == 400
            live.state["prices"]["NSE_EQ|EYE1"] = 500.5               # just above the HTF demand 500 to 480
            first = c.get("/api/eye?mode=DAILY").json()                # no saved row yet: it is saved in the background
            assert first["waiting"] >= 1 and all(r["instrument_key"] != "NSE_EQ|EYE1" for r in first["rows"])
            for _ in range(50):
                d = c.get("/api/eye?mode=DAILY").json()
                if any(r["instrument_key"] == "NSE_EQ|EYE1" for r in d["rows"]):
                    break
                time.sleep(0.1)
            row = next(r for r in d["rows"] if r["instrument_key"] == "NSE_EQ|EYE1")
            assert row["price"] == 500.5 and row["demand"]["state"] == "approaching" and row["demand"]["proximal"] == 500.0
            assert row["supply"]["proximal"] == 620.0 and row["is_index"] is False and "atr_pct" in row and "iv_pct" in row
            assert d["frames"] == ["1d", "1h", "30m"] and d["approach_pct"] == 1.0
            intraday = c.get("/api/eye?mode=30M").json()               # the intraday modes read the same saved rows
            assert intraday["frames"] == ["30m", "15m", "5m"]
            assert any(r["instrument_key"] == "NSE_EQ|EYE1" for r in intraday["rows"])
            with SessionLocal() as db:
                assert {r.mode for r in db.scalars(select(EyeRow).where(EyeRow.instrument_key == "NSE_EQ|EYE1"))} == set(eye.EYE_MODES)
            for page, mark in (("/eye", 'data-current="eye"'), ("/premium", 'id="f-measure"')):
                html = c.get(page).text
                assert mark in html and "/static/js/eyecommon.js" in html
            assert '["eye", "GTF EYE"], ["premium", "Premium"]' in c.get("/static/js/common.js").text
    finally:
        live.state["prices"].pop("NSE_EQ|EYE1", None)
        with SessionLocal() as db:
            settings_store.put(db, "universe", universe)


# ---------------------------------------------------------------- CSV downloads and deleting runs
def test_scan_check_csv_and_backtest_delete():
    with TestClient(app) as c:
        out = c.post("/api/scancheck", json={"symbols": [KEY], "mode": "HOURLY", "scan_date": "2026-05-10", "save": True}).json()
        assert out["trail"]["trail_method"] == "off"
        assert c.get(f"/api/scancheck/{out['id']}").json()["trail"]["trail_method"] == "off"
        r = c.get(f"/api/scancheck/{out['id']}/export")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(r.text)))
        assert len(rows) == 3 and {x["entry_type"] for x in rows} == {"type1", "type2", "type3"}
        assert c.get("/api/scancheck/999999/export").status_code == 404
        assert c.delete(f"/api/backtest/{out['id']}").status_code == 404        # a scan check is deleted on its own route
        with SessionLocal() as db:
            run = BacktestRun(status="done", created_at=datetime(2026, 6, 1), settings={"kind": "range"}, summary={})
            db.add(run)
            db.flush()
            db.add(BacktestTrade(run_id=run.id, instrument_key=KEY, timeframe="5m", side="demand", entry_ts=datetime(2026, 6, 1),
                                 score=7.0, entry=100.0, stop=99.0, target=102.0))
            db.commit()
            rid = run.id
        assert c.delete(f"/api/backtest/{rid}").json() == {"deleted": rid}
        with SessionLocal() as db:
            assert db.get(BacktestRun, rid) is None and not db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == rid)).all()
        assert 'id="s-csv"' in c.get("/backtest").text and 'href="/api/paper/export"' in c.get("/paper").text


# ---------------------------------------------------------------- scan workers
def test_scan_workers_fall_back_to_a_thread_on_sqlite(monkeypatch):
    assert scanpool.workers() == 0                                       # SQLite: one writer at a time
    monkeypatch.setattr(scanpool, "IS_SQLITE", False)
    monkeypatch.setenv("SCAN_WORKERS", "9")
    assert scanpool.workers() == scanpool.MAX_WORKERS
    monkeypatch.setenv("SCAN_WORKERS", "0")
    assert scanpool.workers() == 0
    monkeypatch.setenv("SCAN_WORKERS", "")
    assert 1 <= scanpool.workers() <= scanpool.MAX_WORKERS
    monkeypatch.setattr(scanpool, "IS_SQLITE", True)
    stats = asyncio.run(scanpool.scan(KEY, True))
    assert isinstance(stats, list)
    assert asyncio.run(scanpool.map_heavy(lambda a, b: a + b, [(1, 2), (3, 4)])) == [3, 7]
