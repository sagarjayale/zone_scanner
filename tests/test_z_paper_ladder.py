"""Paper page: the lock ladder of open positions, how near pending orders are to their entry, the wait status of entry
types 2 and 3, tick sizes, and limit orders that fill at the open after a gap."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import live, settings_store
from app.data.sync import upsert_candles
from app.db import SessionLocal
from app.jobs.scheduler import tick_size_rupees
from app.main import app
from app.models import Instrument, PaperOrder, PaperTrade, Setting, Zone
from app.sim import fills, paper, trailing
from tests.test_z_other_ltf_zones import build, seed

LOCK = trailing.options({"trail_method": "lock", "trail_lock_pct": 25, "trail_lock_gap_pct": 20, "trail_lock_target": "move"})
NODE = shutil.which("node") or "/opt/node22/bin/node"
JS = Path(__file__).parent.parent / "app" / "web" / "js"


def _unset(db, *keys):
    for k in keys:
        row = db.get(Setting, k)
        if row is not None:
            db.delete(row)
    db.commit()


# ---------------------------------------------------------------- the ladder (the example of the plan)
def test_ladder_steps_from_the_entry_to_the_target_and_beyond():
    # entry 100, stop 95, target 140, lock 25 %, gap 20 %: step 10, gap 2, 4 steps to the target
    lad = trailing.ladder("demand", 100, 95, 140, LOCK, stop=95, target=140, best=108)
    assert (lad["step"], lad["gap"], lad["steps"], lad["steps_to_target"]) == (10, 2, 0, 4)
    assert lad["levels"] == [110, 120, 130, 140] and (lad["next_stop"], lad["next_trigger"]) == (110, 112)
    assert not lad["target_reached"] and lad["target_mode"] == "move"
    lad = trailing.ladder("demand", 100, 95, 140, LOCK, stop=120, target=140, best=125)
    assert (lad["steps"], lad["next_stop"], lad["next_trigger"], lad["beyond_target"]) == (2, 130, 132, 0)
    lad = trailing.ladder("demand", 100, 95, 140, LOCK, stop=150, target=160, best=153)       # beyond the first target
    assert (lad["steps"], lad["beyond_target"], lad["target_reached"]) == (5, 1, True)
    assert lad["levels"] == [110, 120, 130, 140, 150, 160] and lad["target"] == 160
    short = trailing.ladder("supply", 200, 205, 160, LOCK, stop=190, target=160, best=187)    # a short, mirrored
    assert (short["steps"], short["next_stop"], short["next_trigger"]) == (1, 180, 178)
    assert trailing.ladder("demand", 100, 95, 140, {**LOCK, "trail_lock_pct": 10}, stop=95)["steps_to_target"] == 10
    assert trailing.ladder("demand", 100, 95, 140, {**LOCK, "trail_lock_pct": 50}, stop=95)["steps_to_target"] == 2


def test_the_trailer_and_the_ladder_agree():
    t = trailing.Trailer("demand", 100, 95, LOCK, target=140)
    for best in (108, 113, 125, 133, 141, 143, 153):
        t.close_bar(best, best - 3, best - 1)
        lad = trailing.ladder("demand", 100, 95, 140, LOCK, stop=t.stop, target=t.target, best=t.best)
        assert lad["levels"][lad["steps"] - 1] == pytest.approx(t.stop) if lad["steps"] else t.stop == 95
    assert t.stop == pytest.approx(150) and t.target == pytest.approx(160)


# ---------------------------------------------------------------- server: positions and orders
def test_positions_carry_the_ladder_wait_status_and_tick(monkeypatch):
    key, now = "NSE_EQ|LAD1", datetime(2026, 6, 2, 10, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        db.merge(Instrument(instrument_key=key, symbol="LAD1", segment="NSE_EQ", tick_size=0.1))
        for k, v in (("trail_method", "lock"), ("trail_lock_pct", 25), ("trail_lock_gap_pct", 20)):
            settings_store.put(db, k, v)
        try:
            order = paper.place(db, a, now, "DAILY")
            trade = paper.enter_now(db, order.id, 525.0, now)
        finally:
            _unset(db, "trail_method", "trail_lock_pct", "trail_lock_gap_pct")
        pend = paper.place(db, b, now, "DAILY")                  # entry type 3 order on zone B
        zb = db.get(Zone, b)
        zb.in_visit = True
        db.commit()
        tid, oid = trade.id, pend.id
        entry, stop0, target0 = trade.fill_price, trade.stop, trade.target
    with TestClient(app) as client:
        monkeypatch.setitem(live.state["prices"], key, entry + 1)
        d = client.get("/api/paper/positions").json()
        t = next(x for x in d["open"] if x["id"] == tid)
        v = t["trail_view"]
        step = (target0 - entry) * 0.25
        assert t["tick"] == 0.1 and v["method"] == "lock" and v["steps"] == 0 and v["steps_to_target"] == 4
        assert v["next_stop"] == pytest.approx(entry + step, abs=1e-3) and v["next_trigger"] == pytest.approx(entry + 1.2 * step, abs=1e-3)
        assert v["locked_r"] == -1.0 and v["first_stop"] == stop0
        o = next(x for x in d["pending"] if x["id"] == oid)
        assert o["wait"] == "Price is in the zone: waits for a candle to close inside it" and o["tick"] == 0.1
        assert "market_open" in d
    with SessionLocal() as db:                                   # lock step 2 by hand (as the trailer would) and look again
        tr = db.get(PaperTrade, tid)
        tr.stop = round(entry + 2 * step, 2)
        db.get(Zone, b).inside_armed = True
        db.commit()
    with TestClient(app) as client:
        d = client.get("/api/paper/positions").json()
        v = next(x for x in d["open"] if x["id"] == tid)["trail_view"]
        assert v["steps"] == 2 and v["locked_r"] > 0 and v["locked_inr"] > 0 and v["locked_after_charges"] < v["locked_inr"]
        assert next(x for x in d["pending"] if x["id"] == oid)["wait"].startswith("Armed")
    with SessionLocal() as db:
        for model in (PaperTrade, PaperOrder):
            for row in db.scalars(select(model).where(model.instrument_key == key)):
                db.delete(row)
        db.commit()


def test_tick_size_from_the_instrument_file():
    assert tick_size_rupees(5.0) == 0.05 and tick_size_rupees(10) == 0.1 and tick_size_rupees(0.05) == 0.05
    assert tick_size_rupees(None) is None and tick_size_rupees("x") is None and tick_size_rupees(0) is None


# ---------------------------------------------------------------- gap at the open
def test_limit_fills_at_the_open_after_a_gap():
    assert fills.limit_fill_price("demand", 100, 97) == 97 and fills.limit_fill_price("demand", 100, 101) == 100
    assert fills.limit_fill_price("supply", 200, 204) == 204 and fills.limit_fill_price("supply", 200, 198) == 200
    key, now = "NSE_EQ|GAP1", datetime(2026, 6, 2, 10, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        order = paper.place(db, a, now, "DAILY")                  # buy limit at 521, stop 514
        oid = order.id
        day = datetime(2026, 6, 3, 9, 15)
        upsert_candles(db, key, "5m", [{"ts": day, "open": 517.0, "high": 519.0, "low": 516.0, "close": 518.0, "volume": 1, "oi": 0}])
        paper.on_candles(db, key)
        t = db.scalars(select(PaperTrade).where(PaperTrade.order_id == oid)).one()
        assert t.fill_price == 517.0 and t.risk == pytest.approx(3.0) and t.exit_ts is None    # filled at the open, not at 521
        clear(db, key)
        o2 = paper.place(db, a, day + timedelta(hours=7), "DAILY")          # placed after the first gap day
        upsert_candles(db, key, "5m", [{"ts": day + timedelta(days=1), "open": 510.0, "high": 512.0, "low": 508.0, "close": 511.0, "volume": 1, "oi": 0}])
        paper.on_candles(db, key)
        assert db.scalars(select(PaperTrade).where(PaperTrade.order_id == o2.id)).first() is None   # opened beyond the stop
        o2 = db.get(PaperOrder, o2.id)
        assert o2.status == "cancelled" and o2.note == "price opened past the stop (510): not filled"
        clear(db, key)
        o3 = paper.place(db, a, now, "DAILY")
        paper.on_prices(db, {key: 518.0}, datetime(2026, 6, 5, 9, 16))       # first minutes: a gap, fills at the price
        t3 = db.scalars(select(PaperTrade).where(PaperTrade.order_id == o3.id)).one()
        assert t3.fill_price == 518.0
        clear(db, key)
        o4 = paper.place(db, a, now, "DAILY")
        paper.on_prices(db, {key: 518.0}, datetime(2026, 6, 5, 11, 0))       # later in the day too: the real price
        assert db.scalars(select(PaperTrade).where(PaperTrade.order_id == o4.id)).one().fill_price == 518.0
        clear(db, key)


def clear(db, key):
    for model in (PaperTrade, PaperOrder):
        for row in db.scalars(select(model).where(model.instrument_key == key)):
            db.delete(row)
    db.commit()


# ---------------------------------------------------------------- the page logic (node)
def _node(script: str):
    if not Path(NODE).exists():
        pytest.skip("node is not installed")
    head = ("const fmtPrice = (n) => String(n); const signed = (n) => (n > 0 ? '+' : '') + n;\n" + (JS / "paperview.js").read_text())
    out = subprocess.run([NODE, "-e", head + "\n" + script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_distance_groups_and_speed_in_the_page():
    got = _node("""
const buy = { side: "demand", entry: 100, stop: 95 }, sell = { side: "supply", entry: 200, stop: 205 };
const g = (o, p) => { const d = entryDistance(o, p); return [d.group, d.r, d.at]; };
const h = {}, t0 = 1e12;
for (const [s, p] of [[0, 112], [60, 110], [120, 108], [180, 106]]) trackPrice(h, "X", p, t0 + s * 1000);
const closer = entrySpeed(buy, h.X, t0 + 180000);
const away = entrySpeed(buy, [{ t: t0, price: 103 }, { t: t0 + 90000, price: 106 }], t0 + 90000);
const flat = entrySpeed(buy, [{ t: t0, price: 103 }, { t: t0 + 90000, price: 103.1 }], t0 + 90000);
const early = entrySpeed(buy, [{ t: t0, price: 103 }], t0 + 30000);
const up = entrySpeed(sell, [{ t: t0, price: 190 }, { t: t0 + 120000, price: 195 }], t0 + 120000);
console.log(JSON.stringify({ a: g(buy, 101), b: g(buy, 103), c: g(buy, 112), d: g(buy, 120), e: g(buy, 99), f: g(sell, 199),
  s: g(sell, 190), closer: [closer.kind, closer.text], away: away.text, flat: flat.text, early: early.text, up: up.text, tick: tickPrice(100.03, 0.05) }));
""")
    assert got["a"] == ["now", 0.2, False] and got["b"] == ["close", 0.6, False]
    assert got["c"] == ["between", 2.4, False] and got["d"] == ["far", 4, False]
    assert got["e"] == ["now", 0, True] and got["f"] == ["now", 0.2, False] and got["s"] == ["between", 2, False]
    assert got["closer"][0] == "closer" and got["closer"][1].startswith("⬇ coming closer, ~")       # 1.2 R in 3 min: ~2 min more
    assert got["away"] == "⬆ moving away" and got["flat"] == "➖ flat" and got["early"] == "measuring…"
    assert got["up"].startswith("⬆ coming closer") and got["tick"] == 100.05


def test_ladder_lines_in_the_page():
    got = _node("""
const view = (stop, best, target, reached) => ({ side: "demand", fill_price: 100, stop, target, trail_view: { method: "lock", step: 10, gap: 2,
  steps: Math.max(0, Math.floor((stop - 100) / 10)), steps_to_target: 4, beyond_target: Math.max(0, Math.floor((stop - 100) / 10) - 4),
  levels: [110, 120, 130, 140, 150, 160].slice(0, Math.max(4, Math.floor((stop - 100) / 10) + 1)), next_stop: 100 + (Math.max(0, Math.floor((stop - 100) / 10)) + 1) * 10,
  next_trigger: 102 + (Math.max(0, Math.floor((stop - 100) / 10)) + 1) * 10, target_reached: reached, target_mode: "move", target, risk: 5,
  locked_r: (stop - 100) / 5, locked_inr: (stop - 100) * 200, locked_after_charges: (stop - 100) * 200 - 100 } });
const a = trailLines(view(95, 108, 140, false), 108, 0.05), b = trailLines(view(120, 125, 140, false), 125, 0.05);
const c = trailLines(view(120, 125, 140, false), 121, 0.05), d = trailLines(view(150, 153, 160, true), 153, 0.05);
const e = trailLines(view(120, 133, 140, false), 133, 0.05), f = trailLines(view(95, 98, 140, false), 98, 0.05);
console.log(JSON.stringify({ a: a.lines, b: b.lines, bl: b.locked, c: c.nearStop, bn: b.nearStop, d: [d.lines[0], d.gold], e: e.lines[2],
  dot: f.ladder.losing, fill: b.ladder.filled, ticks: b.ladder.ticks.map((x) => x.kind) }));
""")
    assert got["a"][0] == "🔓 Not locked yet · Step 0 of 4" and got["a"][1] == "stop 95: risk -1 R"
    assert got["a"][2] == "Next: step 1 → stop 110 when price reaches 112 (3.7% away)"
    assert got["b"][0] == "🔒 Step 2 of 4 locked · stop 120" and got["b"][1].startswith("keeps +4 R, +4000 INR (after charges about +3900)")
    assert got["bl"] is True and got["c"] is True and got["bn"] is False          # 121 is within 0.3 R (1.5) of the stop 120
    assert got["d"][0] == "🔒 Step 5 (1 beyond target) of 4 locked · stop 150" and got["d"][1] == ["🎯 First target reached · running beyond target"]
    assert got["e"] == "Next: step 3 → stop 130 when price reaches 132 (reached: locks at the next candle close)"
    assert got["dot"] is True and got["fill"] == 0.5 and got["ticks"] == ["locked", "locked", "next", "step"]


# ---------------------------------------------------------------- the last stored close when there is no live price
def test_last_close_is_sent_when_there_is_no_live_price(monkeypatch):
    key, now = "NSE_EQ|LAST1", datetime(2026, 6, 2, 10, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        order = paper.place(db, b, now, "DAILY")
        o2 = paper.place(db, a, now, "DAILY")
        trade = paper.enter_now(db, o2.id, 525.0, now)
        upsert_candles(db, key, "5m", [{"ts": datetime(2026, 6, 5, 15, 25), "open": 522.0, "high": 524.0, "low": 521.5, "close": 523.5,
                                        "volume": 1, "oi": 0}])
        oid, tid = order.id, trade.id
    monkeypatch.setattr(live, "market_open", lambda now: False)
    live.state["prices"].pop(key, None)
    with TestClient(app) as client:
        d = client.get("/api/paper/positions").json()
        o = next(x for x in d["pending"] if x["id"] == oid)
        t = next(x for x in d["open"] if x["id"] == tid)
        assert d["market_open"] is False and o["price"] is None and t["price"] is None
        assert (o["last_close"], o["last_close_ts"]) == (523.5, "2026-06-05T15:30:00")          # the 15:25 candle closes at 15:30
        assert t["last_close"] == 523.5
        js = client.get("/static/js/paper.js").text
        assert "last_close" in js and "closeLabel" in js
    with SessionLocal() as db:
        for model in (PaperTrade, PaperOrder):
            for row in db.scalars(select(model).where(model.instrument_key == key)):
                db.delete(row)
        db.commit()


# ---------------------------------------------------------------- trailing applied to a position filled without it
def test_apply_trailing_to_an_open_position():
    key, now = "NSE_EQ|APPLY1", datetime(2026, 6, 2, 10, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        trade = paper.enter_now(db, paper.place(db, a, now, "DAILY").id, 525.0, now)     # trailing off in Settings
        tid, entry, stop0, target0 = trade.id, trade.fill_price, trade.stop, trade.target
        assert trade.trail is None
        with pytest.raises(ValueError, match="Trailing is off"):
            paper.apply_trailing(db, tid, now)
    with TestClient(app) as client:
        assert client.post(f"/api/paper/positions/{tid}/trail").status_code == 400
        with SessionLocal() as db:
            for k, v in (("trail_method", "lock"), ("trail_lock_pct", 10), ("trail_lock_gap_pct", 10)):
                settings_store.put(db, k, v)
        try:
            r = client.post(f"/api/paper/positions/{tid}/trail")
            assert r.status_code == 200 and r.json()["trail"]["trail_method"] == "lock"
            v = next(x for x in client.get("/api/paper/positions").json()["open"] if x["id"] == tid)["trail_view"]
            assert v["method"] == "lock" and v["steps_to_target"] == 10 and v["first_stop"] == stop0
            assert v["next_stop"] == pytest.approx(entry + (target0 - entry) * 0.1, abs=1e-3)
            assert client.post("/api/paper/positions/999999/trail").status_code == 400
        finally:
            with SessionLocal() as db:
                _unset(db, "trail_method", "trail_lock_pct", "trail_lock_gap_pct")
                for model in (PaperTrade, PaperOrder):
                    for row in db.scalars(select(model).where(model.instrument_key == key)):
                        db.delete(row)
                db.commit()
