"""Live mode: schema upgrade, price checks and alerts, the bar close job, and the live APIs.
The file name sorts after test_sync_scan on purpose: it reuses those candles."""
import asyncio
from datetime import datetime, time

from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from app import live, settings_store
from app.data import sync as data_sync
from app.db import SessionLocal, engine, ensure_schema
from app.jobs import scheduler as jobs
from app.main import app
from app.models import Job, SyncState, TradePlan, Zone, ZoneEvent
from tests.test_sync_scan import KEY, FakeClient

LIVE_KEY = "NSE_EQ|LIVE"


class LiveFake(FakeClient):
    """Adds today's intraday candles and last traded prices to the fake client."""
    def __init__(self, today=None, prices=None):
        super().__init__()
        self.today, self.prices, self.ltp_calls = today, prices or {}, 0

    async def intraday(self, key, unit, interval):
        return self._bars(self.today, interval) if self.today else []

    async def ltp(self, keys):
        self.ltp_calls += 1
        return {k: self.prices[k] for k in keys if k in self.prices}

    async def profile(self):
        return {"status": "success"}


def test_schema_upgrade_adds_a_missing_column():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN progress"))
    assert "progress" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
    ensure_schema()
    assert "progress" in {c["name"] for c in inspect(engine).get_columns("jobs")}
    ensure_schema()   # running it again changes nothing


def make_setup(db) -> int:
    """A fresh 1-hour demand zone with a pending plan in WEEKLY mode (week, day, 1 hr)."""
    z = Zone(instrument_key=LIVE_KEY, timeframe="1h", side="demand", pattern="DBR", proximal=521.0, distal=514.0,
             base_start_ts=datetime(2026, 10, 1, 10, 15), base_count=2, created_ts=datetime(2026, 10, 1, 12, 15), atr=0.0,
             peak_distance=0.0, touches=0, in_visit=False, status="fresh", score=8.0, label="TYPE_1", has_gap=False,
             legout_body=0.88, legout_close=544.0, legin_explosive=True)
    db.add(z)
    db.flush()
    db.add(TradePlan(zone_id=z.id, version=1, entry=521.0, stop=512.0, target=557.0, reward_risk=4.0, status="pending",
                     is_current=True, allowed=True, trend="UP", mode="WEEKLY",
                     context={"mode": "WEEKLY", "curve": "LOW", "htf_trend": "SIDEWAYS", "itf_trend": "UP", "market_state": "NORMAL", "side": "BUY"}))
    db.commit()
    return z.id


class Page:
    def __init__(self):
        self.got = []

    async def send_json(self, message):
        self.got.append(message)


def test_alerts_fire_once_each_and_reach_the_page():
    page = Page()
    live.hub.clients.add(page)
    with SessionLocal() as db:
        zid = make_setup(db)
        settings_store.put(db, "universe", [LIVE_KEY])
    now = data_sync.now_ist()
    client = LiveFake(prices={LIVE_KEY: 550.0})
    assert asyncio.run(live.poll_once(client, now)) == []                          # more than 1% away: nothing
    client.prices[LIVE_KEY] = 525.0
    first = asyncio.run(live.poll_once(client, now))
    assert [(a["alert"], a["zone_id"], a["entry"], a["stop"], a["target"]) for a in first] == [("approaching", zid, 521.0, 512.0, 557.0)]
    assert asyncio.run(live.poll_once(client, now)) == []                          # same state: no repeat
    client.prices[LIVE_KEY] = 520.5
    assert [a["alert"] for a in asyncio.run(live.poll_once(client, now))] == ["triggered"]
    assert asyncio.run(live.poll_once(client, now)) == []
    with SessionLocal() as db:
        assert db.scalars(select(TradePlan).where(TradePlan.zone_id == zid)).one().status == "triggered"
        kinds = [e.event_type for e in db.scalars(select(ZoneEvent).where(ZoneEvent.zone_id == zid).order_by(ZoneEvent.id))]
        assert kinds == ["alert_approaching", "alert_triggered"]
    assert [m["type"] for m in page.got].count("alert") == 2 and page.got[0]["type"] == "prices"
    assert live.state["prices"][LIVE_KEY] == 520.5 and live.state["last_poll"] == now
    live.hub.clients.discard(page)


def test_alert_goes_straight_to_triggered_when_price_gaps_into_the_zone():
    with SessionLocal() as db:
        zid = make_setup(db)
    alerts = asyncio.run(live.poll_once(LiveFake(prices={LIVE_KEY: 519.0}), data_sync.now_ist()))
    assert [(a["alert"], a["zone_id"]) for a in alerts] == [("triggered", zid)]


def test_which_timeframes_closed_and_market_hours():
    at = lambda h, m: datetime(2026, 10, 5, h, m, 15)
    assert jobs.closed_timeframes(at(10, 11)) == ["5m"] and jobs.closed_timeframes(at(10, 16)) == ["5m", "30m"]
    assert jobs.closed_timeframes(at(10, 46)) == ["5m", "30m"] and jobs.closed_timeframes(at(15, 31)) == ["5m", "30m"]
    assert live.market_open(at(9, 15)) and not live.market_open(at(15, 31)) and not live.market_open(datetime(2026, 10, 4, 11, 0))
    assert live.next_bar_close(at(10, 11)) == datetime(2026, 10, 5, 10, 15) and live.next_bar_close(at(15, 31)) is None
    assert live.next_bar_close(datetime(2026, 10, 5, 8, 0)) == datetime(2026, 10, 5, 9, 20)


def test_bar_close_job_syncs_only_today_and_scans():
    now = datetime(2026, 10, 5, 10, 46, 15)      # Monday; the stored 5-minute marker is 10:25 from the earlier sync test
    with SessionLocal() as db:
        settings_store.put(db, "universe", [KEY])
        assert db.get(SyncState, (KEY, "5m")).last_candle_ts == datetime(2026, 10, 5, 10, 25)
    client, page = LiveFake(today=now.date()), Page()
    live.hub.clients.add(page)
    job_id = asyncio.run(jobs.bar_close(client, now))
    live.hub.clients.discard(page)
    assert client.calls == []                                      # no history request: the intraday call was enough
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert (job.type, job.status, job.error) == ("bar close", "done", None)
        assert db.get(SyncState, (KEY, "5m")).last_candle_ts == datetime(2026, 10, 5, 10, 40)
        assert db.get(SyncState, (KEY, "30m")).last_candle_ts == datetime(2026, 10, 5, 10, 15)
        assert db.get(SyncState, (KEY, "1h")).last_candle_ts == datetime(2026, 10, 5, 9, 15)
        assert db.get(SyncState, (KEY, "5m")).last_scanned_ts == datetime(2026, 10, 5, 10, 40)
    assert live.state["last_scan"] == now and page.got[-1]["type"] == "scan"
    assert asyncio.run(jobs.bar_close(client, datetime(2026, 10, 5, 8, 0))) is None      # outside the session: does nothing


def test_failed_job_is_recorded_not_raised():
    class NoToken(LiveFake):
        async def profile(self):
            from app.upstox.client import TokenMissing
            raise TokenMissing("No Upstox access token. Log in first.")
    job_id = asyncio.run(jobs.morning_prep(NoToken()))
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == "failed" and "Log in first" in job.error


def test_live_apis_and_page():
    with SessionLocal() as db:
        settings_store.put(db, "universe", ["NSE_INDEX|Nifty 50", LIVE_KEY])
    with TestClient(app) as c:
        assert c.get("/scanner").status_code == 200 and c.get("/", follow_redirects=False).headers["location"] == "/scanner"
        s = c.get("/api/live/status").json()
        assert {"market_open", "feed", "bias", "alerts", "next_bar_close", "last_scan", "prices", "scheduler"} <= set(s)
        assert s["scheduler"] is False and s["bias"][0]["side"] == "NONE" and s["bias"][0]["curve"] is None   # no index candles here
        assert s["bias_mode"] == "DAILY" and s["modes"]["MONTHLY"] == ["1mo", "1w", "1d"]
        assert [a["alert"] for a in s["alerts"]][:3] == ["triggered", "triggered", "approaching"]
        rows = c.get("/api/setups?include_triggered=true").json()
        hit = [r for r in rows if r["instrument_key"] == LIVE_KEY]
        assert len(hit) == 2 and all(r["plan_status"] == "triggered" and r["triggered_ts"] and r["price"] == 519.0 for r in hit)
        assert [r for r in c.get("/api/setups").json() if r["instrument_key"] == LIVE_KEY] == []
        with c.websocket_connect("/ws/live") as ws:
            assert ws.receive_json()["type"] == "prices"            # last known prices on connect
            c.portal.call(live.hub.broadcast, {"type": "scan", "time": "x"})
            assert ws.receive_json() == {"type": "scan", "time": "x"}
        assert c.post("/api/feed/reconnect").json() == {"running": True}
