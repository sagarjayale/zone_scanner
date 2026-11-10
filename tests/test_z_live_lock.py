"""Lock on live prices (the new default), the stop one step behind, no fill past the stop, the fill at the real price,
and the Paper tables: timeframe, entry time, open for, waited, placed at, signal time, duration and charges."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import settings_store
from app.config import DEFAULTS
from app.db import SessionLocal
from app.main import app
from app.models import PaperOrder, PaperTrade, Setting
from app.sim import paper, trailing
from tests.test_z_other_ltf_zones import build, seed

LOCK = {"trail_method": "lock", "trail_lock_pct": 10, "trail_lock_gap_pct": 10, "trail_tf": "live"}


def _unset(db, *keys):
    for k in keys:
        row = db.get(Setting, k)
        if row is not None:
            db.delete(row)
    db.commit()


def _clear(db, key):
    for model in (PaperTrade, PaperOrder):
        for row in db.scalars(select(model).where(model.instrument_key == key)):
            db.delete(row)
    db.commit()


@pytest.mark.trailing_default
def test_defaults_are_lock_on_live_prices():
    assert DEFAULTS["trail_method"] == "lock" and DEFAULTS["trail_tf"] == "live" and DEFAULTS["trail_lock_behind"] is False
    with SessionLocal() as db:
        opts = trailing.options({k: settings_store.get(db, k) for k in trailing.OPTION_KEYS})
    assert (opts["trail_method"], opts["trail_tf"]) == ("lock", "live")


def test_a_live_price_locks_the_step_at_once():
    # buy 100, stop 95, target 110: one step is 1, the gap 0.1
    t = trailing.Trailer("demand", 100, 95, trailing.options(LOCK), target=110)
    assert t.see_price(101.05) is None and t.stop == 95          # not past step 1 + gap yet
    assert t.see_price(101.1) == pytest.approx(101)              # at once, no candle
    assert t.see_price(100.5) is None and t.stop == pytest.approx(101)    # never back
    s = trailing.Trailer("supply", 100, 105, trailing.options(LOCK), target=90)
    assert s.see_price(97.9) == pytest.approx(98)                # a short: step 2 at once
    atr = trailing.Trailer("demand", 100, 95, trailing.options({**LOCK, "trail_method": "atr"}), target=110)
    assert atr.see_price(120) is None                            # swing and ATR wait for their candles


def test_one_step_behind():
    behind = trailing.options({**LOCK, "trail_lock_behind": True})
    t = trailing.Trailer("demand", 100, 95, behind, target=110, cost_frac=0.001)
    assert t.see_price(101.1) == pytest.approx(100.1)            # step 1: break-even plus charges
    assert t.see_price(102.1) == pytest.approx(101)              # step 2: step 1
    lad = trailing.ladder("demand", 100, 95, 110, behind, 100.1)
    assert (lad["steps"], lad["next_stop"], lad["next_trigger"], lad["behind"]) == (1, 101.0, 102.1, True)
    assert trailing.ladder("demand", 100, 95, 110, behind, 95)["steps"] == 0
    on = trailing.ladder("demand", 100, 95, 110, trailing.options(LOCK), 101)
    assert (on["steps"], on["next_stop"]) == (1, 102.0)


def test_paper_position_locks_on_the_live_price_and_closes_on_the_locked_stop():
    key, now = "NSE_EQ|LIVELOCK1", datetime(2026, 6, 2, 11, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        for k, v in LOCK.items():
            settings_store.put(db, k, v)
        try:
            order = paper.place(db, a, now, "DAILY")             # buy limit 521, stop 514
            paper.on_prices(db, {key: 521.0}, now)
            trade = db.scalars(select(PaperTrade).where(PaperTrade.order_id == order.id)).one()
            assert trade.trail["trail_tf"] == "live" and trade.trail["tf"] == "5m"
            step = (trade.target - 521.0) / 10
            ev = paper.on_prices(db, {key: 521.0 + step * 1.2}, now + timedelta(seconds=20))
            db.refresh(trade)
            assert [e["event"] for e in ev] == ["stop moved"] and trade.stop == pytest.approx(round(round((521.0 + step) / 0.05) * 0.05, 2))
            assert trade.trail["live_best"] == pytest.approx(521.0 + step * 1.2)
            assert paper.on_prices(db, {key: 521.0 + step * 1.1}, now + timedelta(seconds=40)) == []    # not a new best
            ev = paper.on_prices(db, {key: trade.stop - 0.05}, now + timedelta(minutes=1))
            db.refresh(trade)
            assert [e["event"] for e in ev] == ["stopped"] and trade.exit_reason == "trail" and trade.pnl > 0
        finally:
            _unset(db, *LOCK)
            _clear(db, key)


def test_no_fill_past_the_stop_and_a_fill_at_the_real_price():
    key, now = "NSE_EQ|JUMP1", datetime(2026, 6, 2, 11, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        o1 = paper.place(db, a, now, "DAILY")                    # buy limit 521, stop 514
        ev = paper.on_prices(db, {key: 512.0}, now)              # jumped past the stop
        db.refresh(o1)
        assert [e["event"] for e in ev] == ["cancelled"] and o1.status == "cancelled"
        assert o1.note == "price jumped past the stop (512): not filled"
        assert db.scalars(select(PaperTrade).where(PaperTrade.order_id == o1.id)).first() is None
        _clear(db, key)
        o2 = paper.place(db, a, now, "DAILY")
        paper.on_prices(db, {key: 518.0}, now)                   # past the entry, not the stop
        t = db.scalars(select(PaperTrade).where(PaperTrade.order_id == o2.id)).one()
        assert t.fill_price == 518.0 and t.risk == pytest.approx(4.0)
        _clear(db, key)


def test_paper_tables_show_timeframe_times_and_charges():
    key, now = "NSE_EQ|TABLE1", datetime(2026, 6, 2, 11, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        order = paper.place(db, a, now, "DAILY")
        oid = order.id
    with TestClient(app) as client:
        d = client.get("/api/paper/positions").json()
        o = [x for x in d["pending"] if x["id"] == oid][0]
        assert o["mode"] == "DAILY" and o["placed_at"].startswith("2026-06-02T11:00") and o["signal_ts"]
        with SessionLocal() as db:
            paper.on_prices(db, {key: 521.0}, now + timedelta(minutes=70))
            t = db.scalars(select(PaperTrade).where(PaperTrade.order_id == oid)).one()
            tid = t.id
        d = client.get("/api/paper/positions").json()
        t = [x for x in d["open"] if x["id"] == tid][0]
        assert (t["mode"], t["timeframe"], t["entry_type"], t["waited_s"]) == ("DAILY", "30m", "TYPE_1", 4200.0)
        assert t["open_s"] is not None
        with SessionLocal() as db:
            paper.manual_close(db, tid, 530.0, now + timedelta(minutes=112))
        d = client.get("/api/paper/positions").json()
        t = [x for x in d["closed"] if x["id"] == tid][0]
        assert t["open_s"] == 42 * 60 and t["charges"] > 0 and t["pnl_after_charges"] == pytest.approx(t["pnl"] - t["charges"])
        csv_text = client.get("/api/paper/export").text
        head, line = csv_text.splitlines()[0].split(","), [r for r in csv_text.splitlines() if r.startswith(f"{tid},")][0].split(",")
        got = dict(zip(head, line))
        assert (got["mode"], got["waited"], got["duration"]) == ("DAILY", "1h 10m", "0h 42m") and float(got["charges"]) > 0
        page = client.get("/paper").text
        assert "<th>Timeframe</th><th>Side</th><th>Quantity</th><th>Entry time</th><th>Entry price</th><th>Open for</th>" in page
        assert "<th>Placed at</th><th>Signal time</th>" in page and "<th>Duration</th>" in page and "<th>Charges</th>" in page
    with SessionLocal() as db:
        _clear(db, key)
