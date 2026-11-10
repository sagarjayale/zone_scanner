"""Other LTF zones: the valid LTF zones after the default one, saved with each result; NSE costs in R; the switch for
alerts on every zone; and on the Paper page "Enter now at market" and the chart view."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import live, settings_store
from app.data.sync import upsert_candles
from app.db import SessionLocal
from app.engine import costs, plan, result
from app.main import app
from app.models import PaperOrder, PaperTrade, Setting, TradePlan, Zone
from app.sim import paper
from app.config import DEFAULTS

DEFAULT_SLIP = DEFAULTS["paper_slippage_pct"]

T0 = datetime(2026, 6, 1, 9, 15)


def candles(closes, step):
    return [{"ts": T0 + step * n, "open": c - 1, "high": c + 2, "low": c - 2, "close": float(c), "volume": 1, "oi": 0} for n, c in enumerate(closes)]


def zone(key, tf, side, prox, dist, pat, legout_count=2, bases=2):
    return Zone(instrument_key=key, timeframe=tf, side=side, pattern=pat, proximal=prox, distal=dist, base_start_ts=T0, base_count=bases,
                created_ts=T0, atr=0.0, peak_distance=0.0, touches=0, in_visit=False, status="fresh", score=0, has_gap=False,
                legout_body=0.8, legout_count=legout_count, legout_close=prox + 15, legin_explosive=True)


def seed(db, key, extra=True):
    """DAILY mode (HTF day, ITF 1 hr, LTF 30 min). Price 530, HTF demand 500 to 480, ITF up: a buy. LTF demand zones:
    A 521 to 514 scores 7 (the default), B 511 to 506 scores 6, C 504 to 500 scores 5 (five base candles)."""
    upsert_candles(db, key, "1d", candles([550.0] * 25, timedelta(days=1)))
    upsert_candles(db, key, "1h", candles(np.linspace(400, 560, 80), timedelta(hours=1)))
    upsert_candles(db, key, "30m", candles([540.0, 535.0, 530.0], timedelta(minutes=30)))
    zones = [zone(key, "1d", "demand", 500.0, 480.0, "RBR"), zone(key, "1d", "supply", 620.0, 640.0, "DBD"),
             zone(key, "30m", "demand", 521.0, 514.0, "DBR")]
    if extra:
        zones += [zone(key, "30m", "demand", 511.0, 506.0, "DBR", legout_count=1), zone(key, "30m", "demand", 504.0, 500.0, "DBR", legout_count=1, bases=5)]
    db.add_all(zones)
    db.commit()
    return [z.id for z in zones[2:]]


def snap(cmp):
    return {"cmp": cmp, "low": cmp - 1, "high": cmp + 1, "ema": (540.0, 545.0), "times": [T0, T0 + timedelta(minutes=30), T0 + timedelta(minutes=60)]}


def build(db, key):
    plan.refresh(db, key, "30m", snap(530.0), settings_store.thresholds(db, "30m"), T0)
    db.commit()
    return result.build(db, key, "DAILY")


def test_one_zone_gives_an_empty_list_and_the_default_zone_is_unchanged():
    with SessionLocal() as db:
        seed(db, "NSE_EQ|OTHER1", extra=False)
        alone = build(db, "NSE_EQ|OTHER1")
        assert alone["other_ltf_zones"] == [] and alone["signal"]["signal"] == "BUY"
        seed(db, "NSE_EQ|OTHER2")
        many = build(db, "NSE_EQ|OTHER2")
        for k in ("signal", "primary_entry", "entry", "stoploss", "target", "score", "zone_type", "waiting"):
            assert many["signal"][k] == alone["signal"][k], k
        assert (many["ltf_zone"]["proximal"], many["ltf_zone"]["distal"], many["ltf_zone_count"]) == (521.0, 514.0, 3)


def test_the_other_zones_best_first_with_everything_the_page_shows():
    with SessionLocal() as db:
        a, b, c = seed(db, "NSE_EQ|OTHER3")
        r = build(db, "NSE_EQ|OTHER3")
        others = r["other_ltf_zones"]
        assert [(o["zone_id"], o["score"], o["primary_entry"]) for o in others] == [(b, 6.0, "TYPE_3"), (c, 5.0, "TYPE_3")]
        o = others[0]
        assert (o["proximal"], o["distal"], o["timeframe"], o["pattern"], o["zone_type"], o["zone_state"]) == (511.0, 506.0, "30m", "DBR", "TYPE_2_3", "ACTIVE")
        assert (o["entry"], o["stoploss"], o["waiting"], o["watch"], o["target"]) == (None, 506.0, True, 511.0, 521.0)
        assert o["stop_pct"] == round((511 - 506) / 530 * 100, 2) and 0 < o["costs_r"] < 1 and o["can_paper"] is True
        assert o["created_ts"] is not None
        # saved with the result: the dashboard and the chart read it, they never calculate it
        result.save(db, "NSE_EQ|OTHER3", "DAILY", r)
        db.commit()
    with TestClient(app) as client:
        got = client.get("/api/result/NSE_EQ|OTHER3?mode=DAILY").json()
        assert [x["zone_id"] for x in got["other_ltf_zones"]] == [b, c]


def test_alerts_and_automatic_orders_follow_the_switch():
    with SessionLocal() as db:
        a, b, c = seed(db, "NSE_EQ|OTHER4")
        build(db, "NSE_EQ|OTHER4")
        ids = lambda: {z.id for z, _ in live.watch_rows(db) if z.instrument_key == "NSE_EQ|OTHER4"}
        assert ids() == {a}                                   # off: only the default zone
        settings_store.put(db, "ltf_alerts_all_zones", True)
        try:
            assert ids() == {a, b, c}
        finally:
            db.delete(db.get(Setting, "ltf_alerts_all_zones"))
            db.commit()
        flags = {p.zone_id: p.is_default for p in db.scalars(select(TradePlan).where(TradePlan.zone_id.in_([a, b, c])))}
        assert flags == {a: True, b: False, c: False}


def test_nse_round_trip_charges():
    th = {"nse_product": "delivery", "brokerage_per_order": 20.0, "brokerage_pct": 0.03, "stt_delivery_pct": 0.1, "stt_intraday_pct": 0.025,
          "exchange_txn_pct": 0.00297, "sebi_fee_per_crore": 10.0, "gst_pct": 18.0, "stamp_delivery_pct": 0.015, "stamp_intraday_pct": 0.003}
    # 1,00,000 bought and sold: brokerage 20 + 20, STT 200, exchange 5.94, SEBI 0.2, GST 18% of 46.14, stamp 15
    assert costs.round_trip(100000, 100000, th) == pytest.approx(40 + 200 + 5.94 + 0.2 + 0.18 * 46.14 + 15)
    small = costs.round_trip(10000, 10000, th)                     # brokerage 0.03% = 3 per side, lower than 20
    assert small == pytest.approx(6 + 20 + 0.594 + 0.02 + 0.18 * 6.614 + 1.5)
    intra = costs.round_trip(100000, 100000, dict(th, nse_product="intraday"))
    assert intra == pytest.approx(40 + 25 + 5.94 + 0.2 + 0.18 * 46.14 + 3)
    assert costs.costs_in_r(100, 99, "demand", th, 1_000_000, 1.0) == pytest.approx(costs.round_trip(1_000_000, 1_000_000, th) / 10000, abs=1e-3)
    assert costs.costs_in_r(100, 100, "demand", th, 1_000_000, 1.0) is None


def test_enter_now_at_market_and_the_chart_view_details(monkeypatch):
    now = datetime(2026, 6, 2, 10, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, "NSE_EQ|OTHER5")
        build(db, "NSE_EQ|OTHER5")
        order = paper.place(db, b, now, "DAILY")                  # an entry type 3 order on the second zone
        with pytest.raises(ValueError, match="beyond the stop loss"):
            paper.enter_now(db, order.id, 505.0, now)
        trade = paper.enter_now(db, order.id, 515.0, now)
        slip = 515.0 * float(settings_store.get(db, "paper_slippage_pct")) / 100
        fill = round(515.0 + slip, 2)
        assert (trade.fill_price, trade.stop, trade.target) == (fill, 506.0, round(fill + 2 * (fill - 506.0), 2))
        assert db.get(PaperOrder, order.id).status == "filled" and "entered at market" in db.get(PaperOrder, order.id).note
        assert trade.quantity == paper.quantity(db, "NSE_EQ|OTHER5", fill, 506.0)
        with pytest.raises(ValueError, match="No pending order"):
            paper.enter_now(db, order.id, 515.0, now)
        o2 = paper.place(db, a, now, "DAILY")
        tid, oid = trade.id, o2.id
    with TestClient(app) as client:
        d = client.get(f"/api/paper/detail?trade_id={tid}").json()
        assert d["kind"] == "trade" and d["zone"]["proximal"] == 511.0 and d["plan"]["is_default"] is False and d["plan"]["mode"] == "DAILY"
        d = client.get(f"/api/paper/detail?order_id={oid}").json()
        assert d["kind"] == "order" and d["order"]["status"] == "pending" and d["plan"]["is_default"] is True
        monkeypatch.setattr(live, "market_open", lambda now: False)
        r = client.post(f"/api/paper/orders/{oid}/enter")
        assert r.status_code == 400 and "market is closed" in r.json()["detail"]
        monkeypatch.setattr(live, "market_open", lambda now: True)
        monkeypatch.setitem(live.state["prices"], "NSE_EQ|OTHER5", 525.0)
        r = client.post(f"/api/paper/orders/{oid}/enter")                         # the live price from the feed
        assert r.status_code == 200 and r.json()["fill_price"] == round(525.0 * (1 + float(DEFAULT_SLIP) / 100), 2)
        assert client.get("/api/paper/detail?trade_id=999999").status_code == 404
    with SessionLocal() as db:
        for model in (PaperTrade, PaperOrder):
            for row in db.scalars(select(model).where(model.instrument_key == "NSE_EQ|OTHER5")):
                db.delete(row)
        db.commit()


def test_the_pages_work_with_the_dropdown_off_and_the_score_filter_in_the_browser():
    with TestClient(app) as client:
        page = client.get("/scanner").text
        assert 'id="f-other"' in page
        assert 'id="l-other"' in client.get("/chart").text
        assert "/static/js/tradechart.js" in client.get("/paper").text
    node = shutil.which("node") or "/opt/node22/bin/node"
    if not Path(node).exists():
        pytest.skip("node is not installed")
    common = Path(__file__).parent.parent / "app" / "web" / "js" / "common.js"
    script = ("globalThis.document = { getElementById: () => null }; " + common.read_text() +
              "\nconst r = { other_ltf_zones: [{ score: 8 }, { score: 6.5 }, { score: 6 }, { score: 5 }] };"
              "\nconsole.log(JSON.stringify([0, 5, 6, 7, 8].map((m) => { const o = otherLtf(r, m); return [o.shown.length, o.hidden]; })),"
              " JSON.stringify(otherLtf({}, 5)));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    lines = out.stdout.split()
    assert json.loads(lines[0]) == [[0, 0], [4, 0], [3, 1], [1, 3], [1, 3]] and json.loads(lines[1]) == {"shown": [], "hidden": 0}
