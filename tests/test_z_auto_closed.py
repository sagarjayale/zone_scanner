"""Automatic paper orders after a scan while the market is closed, and the "Place automatic orders now" button."""
import asyncio
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import live, settings_store
from app.data.sync import upsert_candles
from app.db import SessionLocal
from app.jobs import scheduler
from app.main import app
from app.models import PaperOrder, PaperTrade, Setting
from app.sim import paper
from tests.test_z_other_ltf_zones import build, seed

SUNDAY = datetime(2026, 6, 7, 16, 0)          # IST, market closed
_real_watch = live.watch_rows


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


def test_auto_run_places_and_says_why_it_skips():
    key, now = "NSE_EQ|AUTO1", datetime(2026, 6, 8, 11, 0)
    with SessionLocal() as db:
        a, b, c = seed(db, key)
        build(db, key)
        rows = [(z, p) for z, p in live.watch_rows(db) if z.instrument_key == key]
        assert [z.id for z, _ in rows] == [a]                 # the best zone of the mode (score 7)
        settings_store.put(db, "paper_auto_min_score", 8)
        try:
            out = paper.auto_run(db, rows, now)
            assert out["placed"] == [] and out["skipped"][0]["why"] == "score 7 is below your auto score 8"
            settings_store.put(db, "paper_auto_min_score", 7)
            out = paper.auto_run(db, rows, now, "placed while the market was closed")
            assert len(out["placed"]) == 1 and (out["placed"][0]["entry"], out["placed"][0]["stop"]) == (521.0, 514.0)
            assert db.get(PaperOrder, out["placed"][0]["order_id"]).note == "placed while the market was closed"
            assert paper.auto_run(db, rows, now)["skipped"][0]["why"] == "an order already exists for this zone"
        finally:
            _unset(db, "paper_auto_min_score")
        _clear(db, key)


def test_orders_placed_after_a_scan_while_closed_and_filled_on_monday(monkeypatch):
    key = "NSE_EQ|CLOSED1"
    with SessionLocal() as db:
        seed(db, key)
        build(db, key)
    monkeypatch.setattr(live, "market_open", lambda now: False)
    monkeypatch.setattr(live, "watch_rows", lambda db: [(z, p) for z, p in _real_watch(db) if z.instrument_key == key])
    try:
        assert asyncio.run(scheduler.auto_after_scan(SUNDAY)) is None          # switches off: nothing placed
        with SessionLocal() as db:
            settings_store.put(db, "paper_auto", True)
            settings_store.put(db, "paper_auto_min_score", 7)
        assert asyncio.run(scheduler.auto_after_scan(SUNDAY)) is None          # auto on, "when closed" still off
        with SessionLocal() as db:
            settings_store.put(db, "paper_auto_closed", True)
        out = asyncio.run(scheduler.auto_after_scan(SUNDAY))
        assert len(out["placed"]) == 1 and out["placed"][0]["entry"] == 521.0
        with SessionLocal() as db:
            o = db.scalars(select(PaperOrder).where(PaperOrder.instrument_key == key)).one()
            assert o.status == "pending" and o.note == "placed while the market was closed, from the scan of Sun 07 Jun 16:00"
            # Monday: the stored 5 minute candles reach the entry; the order fills there
            upsert_candles(db, key, "5m", [{"ts": datetime(2026, 6, 8, 10, 40), "open": 523.0, "high": 524.0, "low": 520.5, "close": 522.0,
                                            "volume": 1, "oi": 0}])
            paper.on_candles(db, key)
            t = db.scalars(select(PaperTrade).where(PaperTrade.order_id == o.id)).one()
            assert t.fill_price == 521.0 and t.exit_ts is None
        monkeypatch.setattr(live, "market_open", lambda now: True)
        assert asyncio.run(scheduler.auto_after_scan(SUNDAY)) is None          # market hours: the live loop does it
        monkeypatch.setattr(live, "market_open", lambda now: False)
        with TestClient(app) as client:                                         # the button, from the saved scan
            r = client.post("/api/paper/auto/run").json()
            assert r["market_open"] is False and r["placed"] == []
            assert r["skipped"][0]["why"] == "an order already exists for this zone" and r["skipped"][0]["symbol"]
            assert client.put("/api/settings", json={"paper_auto_closed": "yes"}).status_code == 400
            assert client.get("/api/paper/summary").json()["auto_closed"] is True
            page = client.get("/paper").text
            assert 'id="auto-closed"' in page and 'id="auto-run"' in page
    finally:
        with SessionLocal() as db:
            _unset(db, "paper_auto", "paper_auto_min_score", "paper_auto_closed")
            _clear(db, key)
