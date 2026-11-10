"""The price round never blocks: database work in a worker thread, prices pushed first, a slow page dropped instead
of holding up the others, automatic option orders in their own task, candle fills shown at once, the feed badge."""
import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

from app import live
from app.main import app


class SlowSocket:
    def __init__(self, delay):
        self.delay, self.got, self.closed = delay, [], False

    async def send_json(self, m):
        await asyncio.sleep(self.delay)
        self.got.append(m)

    async def close(self):
        self.closed = True


def test_a_slow_page_is_dropped_and_does_not_hold_up_the_others(monkeypatch):
    monkeypatch.setattr(live.Hub, "SEND_TIMEOUT", 0.2)
    hub = live.Hub()
    fast, fast2, slow = SlowSocket(0), SlowSocket(0.01), SlowSocket(5)
    hub.clients |= {fast, fast2, slow}

    async def go():
        loop = asyncio.get_running_loop()
        began = loop.time()
        await hub.broadcast({"type": "prices", "data": {}})
        return loop.time() - began
    took = asyncio.run(go())
    assert took < 1.0                                   # not 5 s: the pages are sent to at the same time, with a limit
    assert fast.got and fast2.got and not slow.got and slow not in hub.clients and fast in hub.clients


def test_prices_are_pushed_before_the_database_work(monkeypatch):
    order = []

    class Client:
        async def ltp(self, keys):
            return {k: 100.0 for k in keys}

    async def grab(m):
        order.append(m.get("type"))
    monkeypatch.setattr(live, "_round_keys", lambda now: ["NSE_EQ|A"])

    def apply(prices, now):
        order.append("database")
        return [], [], [], [{"type": "alert", "alert": "x"}], False
    monkeypatch.setattr(live, "_round_apply", apply)
    monkeypatch.setattr(live.hub, "broadcast", grab)
    monkeypatch.setattr(live.telegram, "notify", lambda t: None)
    monkeypatch.setattr(live.telegram, "alert_text", lambda a: "")
    asyncio.run(live.poll_once(Client(), datetime(2026, 6, 2, 10, 0)))
    assert order == ["prices", "database", "alert"]


def test_automatic_option_orders_run_beside_the_price_round(monkeypatch):
    started = []

    class Client:
        async def ltp(self, keys):
            return {k: 100.0 for k in keys}
    monkeypatch.setattr(live, "_round_keys", lambda now: ["NSE_EQ|A"])
    monkeypatch.setattr(live, "_round_apply", lambda prices, now: ([], [], [], [], True))
    monkeypatch.setattr(live, "start_option_auto", lambda client, now, prices: started.append(prices))

    async def nothing(m):
        return None
    monkeypatch.setattr(live.hub, "broadcast", nothing)
    asyncio.run(live.poll_once(Client(), datetime(2026, 6, 2, 10, 0)))
    assert started == [{"NSE_EQ|A": 100.0}]


def test_feed_badge_endpoint_and_pages():
    with TestClient(app) as c:
        f = c.get("/api/live/feed").json()
        assert set(f) >= {"now", "market_open", "last_poll", "error", "age_s", "stale", "pages"}
        js = c.get("/static/js/common.js").text
        assert "/api/live/feed" in js and "live-feed" in js
        paper = c.get("/static/js/paper.js").text
        assert "reloadSoon()" in paper and "}, 30000);" in paper
