"""Paper trading, options, Telegram and the settings APIs.
The file name sorts after test_sync_scan and test_z_live on purpose: it reuses their helpers and data."""
import asyncio
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import live, settings_store
from app.alerts import telegram
from app.data import sync as data_sync
from app.db import SessionLocal
from app.main import app
from app.models import Instrument, OptionSnapshot, PaperOrder, PaperTrade, TradePlan, Zone
from app.options import chain as options
from app.sim import paper
from tests.test_sync_scan import KEY
from tests.test_z_live import LIVE_KEY, LiveFake, make_setup

NOW = datetime(2026, 10, 5, 11, 0)


class Fake(LiveFake):
    def __init__(self, prices=None, chain=None):
        super().__init__(prices=prices)
        self.chain = chain or []

    async def option_chain(self, key, expiry):
        return self.chain

    async def close(self):
        pass


def fresh_setup() -> int:
    with SessionLocal() as db:
        return make_setup(db)   # demand 521 / 514, stop 512, target 557, score 8, allowed


def test_order_size_fill_stop_and_target():
    zid = fresh_setup()
    with SessionLocal() as db:
        order = paper.place(db, zid, NOW)
        assert (order.quantity, order.entry, order.stop, order.target) == (1111, 521.0, 512.0, 557.0)   # 1% of 10 lakh / 9
        with pytest.raises(ValueError, match="already exists"):
            paper.place(db, zid, NOW)
        assert paper.on_prices(db, {LIVE_KEY: 530.0}, NOW) == []
        assert [e["event"] for e in paper.on_prices(db, {LIVE_KEY: 520.5}, NOW)] == ["filled"]
        trade = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
        assert (trade.fill_price, trade.risk, db.get(PaperOrder, order.id).status) == (521.0, 9.0, "filled")
        assert [e["event"] for e in paper.on_prices(db, {LIVE_KEY: 511.0}, NOW)] == ["stopped"]
        db.refresh(trade)
        assert (trade.exit_price, trade.exit_reason, trade.result, trade.pnl, trade.result_r) == (511.0, "stop", "loss", -11110.0, -1.111)
    zid = fresh_setup()
    with SessionLocal() as db:
        paper.place(db, zid, NOW)
        paper.on_prices(db, {LIVE_KEY: 521.0}, NOW)
        assert [e["event"] for e in paper.on_prices(db, {LIVE_KEY: 558.0}, NOW)] == ["target hit"]
        trade = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
        assert (trade.exit_price, trade.result, trade.result_r, trade.pnl) == (557.0, "win", 4.0, 36 * 1111)


def test_pending_order_is_cancelled_when_its_zone_is_violated():
    zid = fresh_setup()
    with SessionLocal() as db:
        order = paper.place(db, zid, NOW)
        db.get(Zone, zid).status = "violated"
        db.commit()
        assert paper.on_prices(db, {LIVE_KEY: 520.0}, NOW) == []
        db.refresh(order)
        assert (order.status, order.note) == ("cancelled", "zone violated")


def test_candles_catch_a_fill_and_stop_that_polling_missed():
    key = "NSE_EQ|PAPER"
    day = datetime(2026, 10, 5)
    bars = [(10, 0, 530, 532, 528, 529), (10, 5, 529, 530, 520, 524), (10, 10, 524, 526, 511, 513), (10, 15, 513, 560, 512, 558)]
    with SessionLocal() as db:
        data_sync.upsert_candles(db, key, "5m", [{"ts": day.replace(hour=h, minute=m), "open": o, "high": hi, "low": lo, "close": c,
                                                  "volume": 1, "oi": 0} for h, m, o, hi, lo, c in bars])
        z = Zone(instrument_key=key, timeframe="5m", side="demand", pattern="DBR", proximal=521.0, distal=514.0,
                 base_start_ts=day, base_count=2, created_ts=day, atr=10.0, peak_distance=30.0, touches=0, in_visit=False, status="fresh", score=8)
        db.add(z); db.flush()
        db.add(TradePlan(zone_id=z.id, version=1, entry=521.0, stop=512.0, target=557.0, reward_risk=4.0, status="pending", is_current=True, allowed=True))
        db.commit()
        paper.place(db, z.id, day.replace(hour=10, minute=0))
        events = paper.on_candles(db, key)
        assert [(e["event"], e["price"]) for e in events] == [("filled", 521.0), ("stopped", 512.0)]     # fill 10:05, stop 10:10
        t = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == z.id)).one()
        assert (t.fill_ts, t.exit_ts, t.exit_price, t.result_r) == (day.replace(hour=10, minute=5), day.replace(hour=10, minute=10), 512.0, -1.0)
        assert paper.on_candles(db, key) == []      # nothing is processed twice


def chain_rows(spot=521.0):
    rows = []
    for k in range(480, 570, 10):
        d = max(0.02, min(0.98, 0.5 + (spot - k) / 80))
        leg = lambda key, delta, ltp, oi: {"instrument_key": key, "market_data": {"ltp": ltp, "volume": 1000, "oi": oi, "bid_price": ltp - 0.05,
                                           "ask_price": ltp + 0.05, "prev_oi": oi - 500}, "option_greeks": {"delta": delta, "iv": 18.0 + abs(k - spot) / 20}}
        rows.append({"expiry": "2026-10-29", "strike_price": float(k), "underlying_key": LIVE_KEY, "underlying_spot_price": spot,
                     "call_options": leg(f"NSE_FO|C{k}", round(d, 2), max(1.0, spot - k + 8), 40000),
                     "put_options": leg(f"NSE_FO|P{k}", round(d - 1, 2), max(1.0, k - spot + 8), 60000)})
    return rows


def test_option_view_suggestion_snapshots_and_iv_percentile():
    zone = type("Z", (), {"side": "demand", "distal": 514.0})()
    with SessionLocal() as db:
        view = options.build_view(db, chain_rows(), zone)
        assert (view["spot"], view["atm"], view["pcr"]) == (521.0, 520.0, 1.5)
        row520 = next(s for s in view["strikes"] if s["strike"] == 520.0)
        assert row520["call"]["oi_change"] == 500 and row520["call"]["liquid"] is True
        assert view["suggestion"]["buy"]["type"] == "call" and 0.5 <= view["suggestion"]["buy"]["delta"] <= 0.6
        assert view["suggestion"]["sell"] == {"type": "put", "strike": 510.0}                       # first strike below the distal line
        supply = options.build_view(db, chain_rows(), type("Z", (), {"side": "supply", "distal": 526.0})())
        assert supply["suggestion"]["buy"]["type"] == "put" and supply["suggestion"]["sell"] == {"type": "call", "strike": 530.0}
        assert options.build_view(db, [])["strikes"] == []
        assert options.save_snapshot(db, LIVE_KEY, "2026-10-29", chain_rows(), NOW) == 18              # 9 strikes, calls and puts
        first = options.iv_percentile(db, LIVE_KEY, NOW)
        assert (first["days"], first["percentile"]) == (1, None)                                      # still building history
        for n in range(1, 25):
            db.add(OptionSnapshot(underlying_key=LIVE_KEY, expiry="x", strike=520, option_type="ATM", ts=NOW - timedelta(days=n), iv=10.0 + n))
        db.commit()
        later = options.iv_percentile(db, LIVE_KEY, NOW)
        assert later["days"] == 25 and later["percentile"] == round(100 * 8 / 24, 1)                  # 8 of 24 earlier days were lower


def test_telegram_text_and_send(monkeypatch):
    a = {"alert": "approaching", "symbol": "XYZ", "timeframe": "1d", "side": "demand", "price": 530.0, "entry": 521.0, "stop": 512.0,
         "target": 557.0, "reward_risk": 4.0, "score": 8}
    assert telegram.alert_text(a) == "XYZ Day demand zone: approaching at 530.00\nEntry type 1 | Entry 521.00 | Stop 512.00 | Target 557.00 | R:R 4.0 | Score 8"
    hit = telegram.alert_text({**a, "alert": "triggered"})
    assert hit.startswith("XYZ Day demand zone: entry reached at 530.00")
    three = telegram.alert_text({**a, "alert": "triggered", "entry_type": "TYPE_3", "score": 6})       # reaching the zone is not yet the entry
    assert three.splitlines()[0].endswith("price is at the zone at 530.00")
    assert three.splitlines()[1] == "Entry type 3: wait for a candle to close inside the zone, then the next candle closes back out of it | Stop 512.00 | Score 6"
    assert "the next candle opens inside it" in telegram.alert_text({**a, "entry_type": "TYPE_2"})
    assert asyncio.run(telegram.send("x")) == (False, "Telegram is not set up: add the bot token and chat id in settings.")
    sent = {}

    class Reply:
        status_code = 200
        text = ""
        def json(self):
            return {"ok": True}

    async def fake_post(self, url, json=None, **kw):
        sent.update(url=url, json=json)
        return Reply()
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    with SessionLocal() as db:
        settings_store.put(db, "telegram_token", "123:abc"); settings_store.put(db, "telegram_chat_id", "42")
    assert asyncio.run(telegram.send("hello")) == (True, "sent")
    assert sent == {"url": "https://api.telegram.org/bot123:abc/sendMessage", "json": {"chat_id": "42", "text": "hello"}}


def wait_job(c, job_id):
    for _ in range(120):
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.25)
    raise AssertionError("job did not finish")


def test_paper_options_and_settings_through_the_api(monkeypatch):
    zid = fresh_setup()
    fake = Fake(prices={LIVE_KEY: 540.0, "NSE_FO|C510": 19.0}, chain=chain_rows())
    monkeypatch.setattr("app.main.client", fake)
    with SessionLocal() as db:
        settings_store.put(db, "universe", [LIVE_KEY])
        db.merge(Instrument(instrument_key="NSE_FO|C510", symbol="LIVE 510 CE 29 OCT 26", segment="NSE_FO", instrument_type="CE", lot_size=75,
                            expiry=datetime(2030, 10, 29, 15, 30), underlying_key=LIVE_KEY, strike=510.0))
        db.merge(Instrument(instrument_key=LIVE_KEY, symbol="LIVECO", name="Live Company", segment="NSE_EQ", instrument_type="EQ"))
        db.commit()
    with TestClient(app) as c:
        for path in ("/paper", "/options", "/settings"):
            assert c.get(path).status_code == 200
        # paper: place, fill through a live poll, edit, close, summary, reset
        order = c.post("/api/paper/orders", json={"zone_id": zid}).json()
        assert order["status"] == "pending" and c.post("/api/paper/orders", json={"zone_id": zid}).status_code == 400
        fake.prices[LIVE_KEY] = 520.0
        c.portal.call(live.poll_once, fake)
        pos = c.get("/api/paper/positions").json()
        mine = [t for t in pos["open"] if t["zone_id"] == zid]
        assert len(mine) == 1 and mine[0]["symbol"] == "LIVECO" and mine[0]["open_pnl"] == round((520.0 - 521.0) * 1111, 2)
        tid = mine[0]["id"]
        assert c.patch(f"/api/paper/positions/{tid}", json={"stop": 600}).status_code == 400            # stop above target
        edited = c.patch(f"/api/paper/positions/{tid}", json={"stop": 515}).json()
        assert edited["stop"] == 515 and edited["history"][0]["field"] == "stop" and edited["history"][0]["old"] == 512
        fake.prices[LIVE_KEY] = 540.0
        c.portal.call(live.poll_once, fake)        # the poller now holds 540, whatever the time of day
        closed = c.post(f"/api/paper/positions/{tid}/close").json()
        assert closed["exit_reason"] == "manual" and abs(closed["exit_price"] - 540.0 * (1 - 0.0005)) < 1e-6 and closed["result"] == "win"
        assert c.post(f"/api/paper/positions/{tid}/close").status_code == 400
        # a paper option position at the live premium, two lots
        opt = c.post("/api/paper/orders", json={"option_key": "NSE_FO|C510", "label": "LIVECO 510 CE", "lots": 2}).json()
        assert (opt["kind"], opt["quantity"], opt["fill_price"]) == ("option", 150, 19.0)
        s = c.get("/api/paper/summary").json()
        assert s["closed_trades"] >= 1 and s["open_positions"] >= 1 and s["balance"] == round(s["capital"] + s["realised"], 2)
        assert c.delete("/api/paper/orders/999999").status_code == 400
        assert c.post("/api/paper/reset").json()["archived"] > 0
        s = c.get("/api/paper/summary").json()
        assert (s["balance"], s["closed_trades"], s["open_positions"], s["pending_orders"]) == (s["capital"], 0, 0, 0)
        # auto mode places an order for a watched setup on the next poll
        zid2 = fresh_setup()
        c.put("/api/settings", json={"paper_auto": True, "paper_auto_min_score": 8})
        fake.prices[LIVE_KEY] = 560.0
        c.portal.call(live.poll_once, fake)
        assert zid2 in [o["zone_id"] for o in c.get("/api/paper/positions").json()["pending"]]
        c.put("/api/settings", json={"paper_auto": False})
        # options
        assert c.get(f"/api/options/{LIVE_KEY}/expiries").json() == ["2030-10-29"]
        view = c.get(f"/api/options/{LIVE_KEY}", params={"zone_id": zid2}).json()
        assert view["expiry"] == "2030-10-29" and view["pcr"] == 1.5 and view["suggestion"]["sell"]["strike"] == 510.0
        assert view["zone"]["id"] == zid2 and view["iv"]["days"] >= 1
        assert c.get("/api/options/NSE_EQ|NOOPTIONS").status_code == 404
        # settings: secrets stay hidden, search, jobs, new instruments are synced, rebuild
        got = c.get("/api/settings").json()
        assert "telegram_token" not in got and got["telegram_configured"] is True
        assert c.post("/api/alerts/test").status_code in (200, 400)        # no network in tests: either answer is handled
        assert c.get("/api/instruments/search", params={"q": "livec"}).json()[0]["instrument_key"] == LIVE_KEY
        out = c.put("/api/settings", json={"universe": [LIVE_KEY, KEY]}).json()
        assert out["sync_job_id"] and wait_job(c, out["sync_job_id"])["status"] == "done"
        assert c.get("/api/jobs").json()[0]["type"] == "new instruments"
        active = lambda d: sorted((z.timeframe, z.side, z.proximal, z.distal, z.touches, z.status) for z in d.scalars(
            select(Zone).where(Zone.instrument_key == KEY, Zone.status.in_(("fresh", "tested")))))
        with SessionLocal() as db:
            before = active(db)
        c.put("/api/settings", json={"universe": [KEY]})
        assert wait_job(c, c.post("/api/rebuild/zones").json()["job_id"])["status"] == "done"
        with SessionLocal() as db:
            after = active(db)
        assert before and after == before        # same candles and rules give the same active zones
