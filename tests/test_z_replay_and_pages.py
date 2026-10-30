"""Replay, pages, split detection and gap repair. The file name sorts after test_sync_scan on purpose: these tests reuse the candles and zones it creates."""
import asyncio
import calendar
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.data import sync as data_sync
from app.db import SessionLocal
from app.main import app
from app.models import Candle, SyncState, Zone
from tests.test_sync_scan import KEY, NOW, FakeClient


def test_pages_and_static_files_load():
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).status_code == 307
        for path in ("/chart", "/data", "/static/js/chart.js", "/static/css/app.css",
                     "/static/vendor/lightweight-charts.standalone.production.js"):
            assert c.get(path).status_code == 200, path
        got = c.get("/api/settings").json()
        assert set(got["symbols"]) == set(got["universe"])      # a display name for every instrument in the list


def test_replay_streams_bars_without_touching_live_tables():
    with SessionLocal() as db:
        zones_before = db.scalar(select(func.count()).select_from(Zone))
    with TestClient(app) as c:
        bad = c.post("/api/replay/start", json={"symbol": KEY, "timeframe": "30m", "start": "2030-01-01T00:00:00"})
        assert bad.status_code == 400 and "last stored candle" in bad.json()["detail"]
        r = c.post("/api/replay/start", json={"symbol": KEY, "timeframe": "30m", "start": "2026-09-01T00:00:00"}).json()
        with c.websocket_connect(f"/ws/replay/{r['session_id']}") as ws:
            init = ws.receive_json()
            assert init["type"] == "init" and len(init["candles"]) == 300 and init["remaining"] == r["remaining"]
            first_replay_bar = calendar.timegm(datetime(2026, 9, 1, 9, 15).timetuple())
            assert init["candles"][-1]["time"] < first_replay_bar
            ws.send_json({"cmd": "step"})
            bar = ws.receive_json()
            assert bar["type"] == "bar" and bar["remaining"] == r["remaining"] - 1
            assert bar["candle"]["time"] == first_replay_bar
            ws.send_json({"cmd": "next_event"})
            seen = 0
            while True:
                m = ws.receive_json()
                seen += 1
                if m["type"] == "end" or m["events"]:
                    break
            assert m["type"] == "bar" and m["events"][0]["type"] in ("created", "touched", "entry2", "entry3", "consumed", "violated")
            for z in m["zones"]:
                assert {"entry", "stop", "target", "score", "created"} <= set(z)
            ws.send_json({"cmd": "play", "speed": 60})
            assert ws.receive_json()["type"] == "bar"      # bars arrive on their own while playing
            ws.send_json({"cmd": "pause"})
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Zone)) == zones_before


def test_replay_matches_the_live_scan():
    """Same engine, same candles: replay to the end must leave the same active zones as the scan did."""
    from app.sim.replay import ReplaySession
    with SessionLocal() as db:
        s = ReplaySession(db, KEY, "1h", datetime(2026, 9, 1))
        while s.next_bar():
            pass
        live = db.scalars(select(Zone).where(Zone.instrument_key == KEY, Zone.timeframe == "1h",
                                             Zone.status.in_(("fresh", "tested")))).all()
        assert sorted((z.side, z.proximal, z.distal, z.touches) for z in s.eng.active) == \
               sorted((z.side, z.proximal, z.distal, z.touches) for z in live)


class GappyClient(FakeClient):
    async def historical(self, key, unit, interval, to_date, from_date):
        rows = await super().historical(key, unit, interval, to_date, from_date)
        return [r for r in rows if not (r["ts"].date() == datetime(2026, 9, 29).date() and r["ts"].hour >= 13)] \
            if unit == "minutes" and not getattr(self, "fixed", False) else rows


def test_gap_is_counted_then_repaired():
    key = "NSE_EQ|GAPPY"
    with SessionLocal() as db:
        client = GappyClient()
        asyncio.run(data_sync.sync_symbol(db, client, key, now=NOW))
        assert db.get(SyncState, (key, "5m")).gaps == 1 and db.get(SyncState, (key, "30m")).gaps == 1
        client.fixed = True
        asyncio.run(data_sync.sync_symbol(db, client, key, now=NOW))                    # normal sync does not look back
        assert db.get(SyncState, (key, "5m")).gaps == 1
        asyncio.run(data_sync.sync_symbol(db, client, key, repair_days=30, now=NOW))
        assert db.get(SyncState, (key, "5m")).gaps == 0 and db.get(SyncState, (key, "30m")).gaps == 0


class SplitClient(FakeClient):
    async def historical(self, key, unit, interval, to_date, from_date):
        rows = await super().historical(key, unit, interval, to_date, from_date)
        return [{**r, **{k: r[k] / 2 for k in ("open", "high", "low", "close")}} for r in rows]


def test_adjusted_history_triggers_full_reset():
    key = "NSE_EQ|SPLIT"
    with SessionLocal() as db:
        asyncio.run(data_sync.sync_symbol(db, FakeClient(), key, now=NOW))
        from app.engine import scanner
        scanner.scan_symbol(db, key, now=NOW)
        old_close = db.get(Candle, (key, "1d", datetime(2026, 10, 1))).close
        assert db.scalar(select(func.count()).where(Zone.instrument_key == key)) > 0
        out = asyncio.run(data_sync.sync_symbol(db, SplitClient(), key, now=NOW + timedelta(days=3)))   # Monday
        assert out.get("history_reset") is True
        db.expire_all()
        assert abs(db.get(Candle, (key, "1d", datetime(2026, 10, 1))).close - old_close / 2) < 1e-9
        assert db.scalar(select(func.count()).where(Zone.instrument_key == key)) == 0    # zones rebuilt on next scan
        assert db.get(SyncState, (key, "1d")).last_scanned_ts is None
