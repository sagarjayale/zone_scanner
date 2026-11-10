"""Option paper trading: the expiry, the strike by each rule, the lots from the risk, the order waiting for the stock,
the fill at the option's price, the stop / target / trailing on the stock's price, the expiry-day close, charges, and
the page and its API."""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import live, settings_store
from app.db import SessionLocal
from app.main import app
from app.models import Instrument, OptionPaperTrade, Setting
from app.sim import optionpaper
from tests.test_z_other_ltf_zones import build, seed

NOW = datetime(2026, 6, 2, 10, 0)          # Tuesday, market open
EXPIRY = "2026-06-25"                      # 23 days away
NEAR_EXP = "2026-06-04"                    # 2 days away: below the 7-day minimum
LOT = 100


def _unset(db, *keys):
    for k in keys:
        row = db.get(Setting, k)
        if row is not None:
            db.delete(row)
    db.commit()


def _leg(key, ltp, delta, liquid=True):
    return {"instrument_key": key, "ltp": ltp, "delta": delta, "gamma": 0.004, "liquid": liquid, "oi": 5000}


def strikes(spot=530.0, under="X"):
    """Strikes 490 to 570, every 10: call delta falls from about 0.9 to 0.1, put delta the other way."""
    out = []
    for k in range(490, 580, 10):
        d = max(0.05, min(0.95, 0.5 + (spot - k) / 80))
        out.append({"strike": float(k), "call": _leg(f"{under}C{k}", round(max(spot - k, 0) + 6, 2), round(d, 2)),
                    "put": _leg(f"{under}P{k}", round(max(k - spot, 0) + 6, 2), round(d - 1, 2))})
    return out


def chain_rows(spot, under):
    """The same strikes in Upstox's option chain format."""
    rows = []
    for s in strikes(spot, under):
        def raw(leg):
            return {"instrument_key": leg["instrument_key"], "market_data": {"ltp": leg["ltp"], "bid_price": leg["ltp"] - 0.05, "ask_price": leg["ltp"] + 0.05,
                                                                            "oi": 5000, "prev_oi": 4000, "close_price": leg["ltp"], "volume": 100},
                    "option_greeks": {"delta": leg["delta"], "gamma": 0.004, "theta": -0.5, "iv": 20}}
        rows.append({"strike_price": s["strike"], "underlying_spot_price": spot, "call_options": raw(s["call"]), "put_options": raw(s["put"])})
    return rows


class FakeClient:
    def __init__(self, spot, under):
        self.spot, self.under = spot, under

    async def option_chain(self, key, expiry):
        return chain_rows(self.spot, self.under)

    async def close(self):
        return None


def contracts(db, key, under):
    db.query(Instrument).filter(Instrument.underlying_key == key).delete()
    for e in (NEAR_EXP, EXPIRY):
        for k in range(490, 580, 10):
            for t in ("CE", "PE"):
                db.add(Instrument(instrument_key=f"{under}{t[0]}{k}" if e == EXPIRY else f"{under}N{t[0]}{k}", symbol=f"{under} {k} {t}",
                                  segment="NSE_FO", instrument_type=t, lot_size=LOT, expiry=datetime.fromisoformat(e), strike=float(k),
                                  underlying_key=key))
    if db.get(Instrument, key) is None:
        db.add(Instrument(instrument_key=key, symbol=under, segment="NSE_EQ"))
    db.commit()


def setup(key, under):
    with SessionLocal() as db:
        seed(db, key)
        build(db, key)
        contracts(db, key, under)
        rows = [(z, p) for z, p in live.watch_rows(db) if z.instrument_key == key]
        return rows[0][1].id


def _clear(db, key):
    for t in db.scalars(select(OptionPaperTrade).where(OptionPaperTrade.underlying_key == key)):
        db.delete(t)
    db.commit()


def test_expiry_and_strike_rules():
    today = date(2026, 6, 2)
    assert optionpaper.pick_expiry([NEAR_EXP, EXPIRY], today, 7) == EXPIRY
    assert optionpaper.pick_expiry([NEAR_EXP, EXPIRY], today, 0) == NEAR_EXP
    assert optionpaper.pick_expiry([NEAR_EXP], today, 7) is None
    s = strikes(530.0)
    row, leg, why = optionpaper.pick_strike(s, "BUY", "delta", 530.0, 550.0)
    assert row["strike"] == 530.0 and leg["delta"] == 0.5 and why == "delta nearest 0.55"       # 0.5 is nearest 0.55 here
    assert optionpaper.pick_strike(s, "BUY", "atm", 532.0, 550.0)[0]["strike"] == 530.0
    assert optionpaper.pick_strike(s, "BUY", "otm1", 530.0, 550.0)[0]["strike"] == 540.0       # a call: one strike above
    assert optionpaper.pick_strike(s, "SELL", "otm1", 530.0, 510.0)[0]["strike"] == 520.0      # a put: one strike below
    row, leg, _ = optionpaper.pick_strike(s, "SELL", "target", 530.0, 512.0)
    assert row["strike"] == 510.0 and leg["instrument_key"] == "XP510"
    s[4]["call"]["liquid"] = False                                                              # 530 CE thin
    assert optionpaper.pick_strike(s, "BUY", "atm", 530.0, 550.0)[0] is None
    assert "not liquid" in optionpaper.pick_strike(s, "BUY", "atm", 530.0, 550.0)[2]


def test_lots_from_the_risk_and_the_premium_cap():
    leg = _leg("K", 10.0, 0.5)
    # stock 530; entry 520 -> premium 10 - 5 + 0.2 = 5.2; stop 510 -> 10 - 10 + 0.8 = 0.8: risk 4.4 a unit, 440 a lot of 100
    out = optionpaper.size(leg, 530.0, 520.0, 510.0, 540.0, 100, budget=1000, max_premium=100000)
    assert (out["est_entry"], out["est_stop"], out["lots"], out["risk_per_lot"]) == (5.2, 0.8, 2, 440.0)
    assert optionpaper.size(leg, 530.0, 520.0, 510.0, 540.0, 100, budget=400, max_premium=100000)["why"].startswith("1 lot risks about ₹440")
    capped = optionpaper.size(leg, 530.0, 520.0, 510.0, 540.0, 100, budget=5000, max_premium=1100)
    assert capped["lots"] == 2 and capped["capped"]                 # 11 lots for the risk, 2 by the premium cap (520 a lot)


def test_place_wait_fill_trail_and_stop():
    key, under = "NSE_EQ|OPT1", "OPA"
    plan_id = setup(key, under)
    with SessionLocal() as db:
        settings_store.put(db, "opt_paper_capital", 1000000.0)
        for k, v in {"trail_method": "lock", "trail_lock_pct": 10, "trail_lock_gap_pct": 20, "trail_tf": "live"}.items():
            settings_store.put(db, k, v)
        try:
            from app.models import TradePlan
            plan = db.get(TradePlan, plan_id)
            assert (plan.entry, plan.stop) == (521.0, 514.0)
            got = asyncio.run(optionpaper.place(db, FakeClient(530.0, under), plan, NOW))
            t = got["placed"]
            assert (t.status, t.signal, t.option_type, t.expiry, t.strike, t.lot_size) == ("pending", "BUY", "CE", EXPIRY, 530.0, LOT)
            assert t.option_key == f"{under}C530" and t.est_entry < 6 and t.est_stop < t.est_entry and t.lots >= 1
            assert t.risk_amount == pytest.approx(round((t.est_entry - t.est_stop) * LOT, 2) * t.lots, rel=1e-6)
            assert "already exists" in asyncio.run(optionpaper.place(db, FakeClient(530.0, under), plan, NOW))["skipped"]
            assert optionpaper.watch_keys(db) >= {key, f"{under}C530"}
            # the stock comes down to the entry; the option has no price yet: wait
            assert optionpaper.on_prices(db, {key: 521.0}, NOW) == [] and t.status == "pending" and "waiting for the option" in t.note
            ev = optionpaper.on_prices(db, {key: 520.8, f"{under}C530": 3.4}, NOW + timedelta(seconds=5))
            assert [e["event"] for e in ev] == ["filled"] and (t.status, t.fill_premium, t.fill_spot) == ("open", 3.4, 520.8)
            assert t.trail["trail_method"] == "lock"
            # the stock rises: lock step 1 on the stock (step = 10% of 520.8 -> 535 = 1.42, gap 0.284)
            step = (t.first_target - 520.8) * 0.1
            ev = optionpaper.on_prices(db, {key: 520.8 + step * 1.25, f"{under}C530": 4.2}, NOW + timedelta(minutes=5))
            assert [e["event"] for e in ev] == ["stop moved"] and t.stop == pytest.approx(round(520.8 + step, 2))
            # back down through the locked stop: the option is sold at its live price
            ev = optionpaper.on_prices(db, {key: t.stop - 0.1, f"{under}C530": 4.0}, NOW + timedelta(minutes=9))
            assert [e["event"] for e in ev] == ["closed"] and (t.exit_reason, t.exit_premium) == ("trail", 4.0)
            assert t.pnl == pytest.approx((4.0 - 3.4) * t.quantity) and t.charges > 40 and t.result_r == pytest.approx((t.pnl - t.charges) / t.risk_amount, abs=1e-3)
        finally:
            _unset(db, "opt_paper_capital", "trail_method", "trail_lock_pct", "trail_lock_gap_pct", "trail_tf")
            _clear(db, key)


def test_no_buy_past_the_stop_the_target_and_the_expiry_close():
    key, under = "NSE_EQ|OPT2", "OPB"
    plan_id = setup(key, under)
    with SessionLocal() as db:
        from app.models import TradePlan
        plan = db.get(TradePlan, plan_id)
        t = asyncio.run(optionpaper.place(db, FakeClient(530.0, under), plan, NOW))["placed"]
        ev = optionpaper.on_prices(db, {key: 512.0, t.option_key: 1.0}, NOW)          # jumped past the stop 514
        assert [e["event"] for e in ev] == ["cancelled"] and t.status == "cancelled" and "jumped past the stop" in t.note
        _clear(db, key)
        t = asyncio.run(optionpaper.place(db, FakeClient(530.0, under), plan, NOW))["placed"]
        optionpaper.on_prices(db, {key: 521.0, t.option_key: 3.5}, NOW)
        assert t.status == "open" and t.trail is None                                # trailing off in the tests
        ev = optionpaper.on_prices(db, {key: t.target + 0.5, t.option_key: 12.0}, NOW + timedelta(hours=1))
        assert t.exit_reason == "target" and t.exit_premium == 12.0
        _clear(db, key)
        t = asyncio.run(optionpaper.place(db, FakeClient(530.0, under), plan, NOW))["placed"]
        optionpaper.on_prices(db, {key: 521.0, t.option_key: 3.5}, NOW)
        last_day = datetime.fromisoformat(EXPIRY).replace(hour=15, minute=14)
        assert optionpaper.on_prices(db, {key: 522.0, t.option_key: 2.0}, last_day) == []
        optionpaper.on_prices(db, {key: 522.0, t.option_key: 1.9}, last_day + timedelta(minutes=1))
        assert (t.status, t.exit_reason, t.exit_premium) == ("closed", "expiry", 1.9)
        _clear(db, key)


def test_hold_when_the_money_is_used_up_and_the_premium_stop():
    key, under = "NSE_EQ|OPT3", "OPC"
    plan_id = setup(key, under)
    with SessionLocal() as db:
        from app.models import TradePlan
        plan = db.get(TradePlan, plan_id)
        settings_store.put(db, "opt_paper_capital", 20000.0)
        settings_store.put(db, "opt_paper_risk_pct", 10.0)
        settings_store.put(db, "opt_max_premium_pct", 100.0)
        try:
            t = asyncio.run(optionpaper.place(db, FakeClient(530.0, under), plan, NOW))["placed"]
            optionpaper.on_prices(db, {key: 521.0, t.option_key: 300.0}, NOW)       # a price far above the estimate: too dear
            assert t.status == "pending" and t.note.startswith("waiting for money")
            settings_store.put(db, "opt_premium_stop_pct", 50.0)
            optionpaper.on_prices(db, {key: 521.0, t.option_key: 4.0}, NOW + timedelta(seconds=5))
            assert t.status == "open"
            optionpaper.on_prices(db, {key: 520.0, t.option_key: 1.9}, NOW + timedelta(minutes=2))   # premium down 52%
            assert (t.status, t.exit_reason) == ("closed", "premium stop")
        finally:
            _unset(db, "opt_paper_capital", "opt_paper_risk_pct", "opt_max_premium_pct", "opt_premium_stop_pct")
            _clear(db, key)


def test_charges():
    th = {"brokerage_per_order": 20, "brokerage_pct": 0.03, "opt_stt_pct": 0.1, "opt_exchange_pct": 0.03503, "sebi_fee_per_crore": 10,
          "gst_pct": 18, "stamp_intraday_pct": 0.003}
    # bought 750 at 10 (7,500), sold at 15 (11,250): brokerage 2.25 + 3.375, STT 11.25, exchange 6.57, GST 2.20, stamp 0.23
    assert optionpaper.charges(7500, 11250, th) == pytest.approx(25.89, abs=0.01)


def test_page_and_api(monkeypatch):
    key, under = "NSE_EQ|OPT4", "OPD"
    plan_id = setup(key, under)
    import app.main as main
    monkeypatch.setattr(main, "client", FakeClient(530.0, under))
    monkeypatch.setattr("app.main.data_sync.now_ist", lambda: NOW)
    with TestClient(app) as c:
        page = c.get("/option-paper").text
        assert "Option paper trading" in page and 'id="signals"' in page and "/static/js/optionpaper.js" in page
        assert '"option-paper", "Option paper"' in c.get("/static/js/common.js").text
        d = c.get("/api/option-paper/positions").json()
        assert any(s["plan_id"] == plan_id and s["signal"] == "BUY" and not s["placed"] for s in d["signals"])
        assert d["summary"]["balance"] == 500000.0 and d["settings"]["opt_strike_rule"] == "delta"
        r = c.post("/api/option-paper/place", json={"plan_id": plan_id})
        assert r.status_code == 200, r.text
        assert r.json()["option_label"] == "OPD 25 JUN 530 CE"
        assert c.post("/api/option-paper/place", json={"plan_id": plan_id}).status_code == 400
        d = c.get("/api/option-paper/positions").json()
        oid = d["pending"][0]["id"]
        assert any(s["plan_id"] == plan_id and s["placed"] for s in d["signals"])
        with SessionLocal() as db:
            optionpaper.on_prices(db, {key: 521.0, f"{under}C530": 3.5}, NOW)
        assert c.get("/api/option-paper/positions").json()["open"][0]["id"] == oid
        assert c.post(f"/api/option-paper/{oid}/close", json={"price": 5.0}).json()["exit_reason"] == "manual"
        csv_text = c.get("/api/option-paper/export").text
        assert csv_text.startswith("id,symbol,option_label") and "OPD 25 JUN 530 CE" in csv_text
        assert c.put("/api/settings", json={"opt_strike_rule": "far"}).status_code == 400
        assert c.put("/api/settings", json={"opt_expiry_close": "16:00"}).status_code == 400
        assert c.put("/api/settings", json={"opt_auto": "yes"}).status_code == 400
        assert c.put("/api/settings", json={"opt_strike_rule": "atm", "opt_expiry_close": "15:10"}).status_code == 200
        assert c.post("/api/option-paper/reset").json()["archived"] >= 1
        assert c.get("/api/option-paper/positions").json()["closed"] == []
        with SessionLocal() as db:
            _unset(db, "opt_strike_rule", "opt_expiry_close")
            _clear(db, key)


def test_price_round_fetches_and_manages_option_positions(monkeypatch):
    """live.poll_once: the option's key is in the price request, and a fill is pushed to the pages."""
    key, under = "NSE_EQ|OPT5", "OPE"
    plan_id = setup(key, under)
    with SessionLocal() as db:
        from app.models import TradePlan
        t = asyncio.run(optionpaper.place(db, FakeClient(530.0, under), db.get(TradePlan, plan_id), NOW))["placed"]
        option_key = t.option_key
    asked, sent = [], []

    class PriceClient(FakeClient):
        async def ltp(self, keys):
            asked.extend(keys)
            return {k: (521.0 if k == key else 3.3) for k in keys}

    async def grab(msg):
        sent.append(msg)
    monkeypatch.setattr(live.hub, "broadcast", grab)
    monkeypatch.setattr(live.telegram, "notify", lambda text: None)
    asyncio.run(live.poll_once(PriceClient(530.0, under), NOW))
    assert option_key in asked and key in asked
    assert any(m.get("type") == "option-paper" and m.get("event") == "filled" for m in sent)
    with SessionLocal() as db:
        assert db.get(OptionPaperTrade, t.id).fill_premium == 3.3
        _clear(db, key)


def test_strike_and_premium_on_every_table_and_the_preview(monkeypatch):
    """Each order shows its strike, the premium now and the estimated premiums at the stock's entry, stop and
    target; the stop's premium is estimated again when the lock moves the stock's stop. Signals show the option they
    would get before placing."""
    key, under = "NSE_EQ|OPT6", "OPF"
    plan_id = setup(key, under)
    import app.main as main
    monkeypatch.setattr(main, "client", FakeClient(530.0, under))
    monkeypatch.setattr("app.main.data_sync.now_ist", lambda: NOW)
    optionpaper._previews.clear()
    with TestClient(app) as c:
        pv = c.get(f"/api/option-paper/previews?plan_ids={plan_id}").json()[str(plan_id)]
        assert c.get("/api/option-paper/previews?plan_ids=x").status_code == 400
        assert (pv["strike"], pv["option_type"], pv["expiry"], pv["days_left"], pv["premium"]) == (530.0, "CE", EXPIRY, 23, 6.0)
        assert pv["est_entry"] < pv["premium"] and pv["est_stop"] < pv["est_entry"] < pv["est_target"] and pv["lots"] >= 1
        assert pv["cost"] == pytest.approx(pv["est_entry"] * LOT * pv["lots"]) and pv["risk"] > 0
        oid = c.post("/api/option-paper/place", json={"plan_id": plan_id}).json()["id"]
        o = c.get("/api/option-paper/positions").json()["pending"][0]
        assert o["id"] == oid and o["strike"] == 530.0 and o["placed_by"] == "you" and o["days_left"] == 23
        assert o["est_entry_now"] == pytest.approx(o["est_entry"]) and o["est_stop_now"] < o["est_entry_now"]
        with SessionLocal() as db:
            settings_store.put(db, "trail_method", "lock")
            settings_store.put(db, "trail_tf", "live")
            optionpaper.on_prices(db, {key: 521.0, f"{under}C530": 3.5}, NOW)
            t = db.get(OptionPaperTrade, oid)
            step = (t.first_target - 521.0) / 10
            optionpaper.on_prices(db, {key: 521.0 + step * 1.5, f"{under}C530": 4.4}, NOW + timedelta(minutes=3))
            _unset(db, "trail_method", "trail_tf")
        t = c.get("/api/option-paper/positions").json()["open"][0]
        assert t["stop"] > t["first_stop"] and t["est_stop_now"] > t["est_stop"]          # the stop's premium moved up with the lock
        assert t["change_pct"] == pytest.approx(25.7, abs=0.1) and t["open_r"] > 0
        page = c.get("/option-paper").text
        assert "<th>Strike</th><th>Bought at</th><th>Premium now</th>" in page and "<th>Buy at (est.)</th>" in page
        assert 'id="opt_auto_closed"' in page and "<th>Premium in → out</th>" in page
    with SessionLocal() as db:
        _clear(db, key)


def test_auto_option_orders_after_a_scan_while_closed(monkeypatch):
    from app.jobs import scheduler
    key, under = "NSE_EQ|OPT7", "OPG"
    setup(key, under)
    sunday = datetime(2026, 6, 7, 16, 0)
    monkeypatch.setattr(live, "market_open", lambda now: False)
    real = live.watch_rows
    monkeypatch.setattr(live, "watch_rows", lambda db: [(z, p) for z, p in real(db) if z.instrument_key == key])
    optionpaper._tried.clear()
    try:
        assert asyncio.run(scheduler.option_auto_after_scan(FakeClient(530.0, under), sunday)) is None      # off
        with SessionLocal() as db:
            settings_store.put(db, "opt_auto", True)
            settings_store.put(db, "opt_auto_min_score", 7)
        assert asyncio.run(scheduler.option_auto_after_scan(FakeClient(530.0, under), sunday)) is None      # "when closed" off
        with SessionLocal() as db:
            settings_store.put(db, "opt_auto_closed", True)
        out = asyncio.run(scheduler.option_auto_after_scan(FakeClient(530.0, under), sunday))
        assert len(out["placed"]) == 1 and out["placed"][0]["label"] == "OPG 25 JUN 530 CE"
        with SessionLocal() as db:
            t = db.scalars(select(OptionPaperTrade).where(OptionPaperTrade.underlying_key == key)).one()
            assert (t.status, t.placed_by) == ("pending", "auto") and t.note == "placed while the market was closed, from the scan of Sun 07 Jun 16:00"
        with TestClient(app) as c:
            import app.main as main
            monkeypatch.setattr(main, "client", FakeClient(530.0, under))
            monkeypatch.setattr("app.main.data_sync.now_ist", lambda: sunday)
            r = c.post("/api/option-paper/auto/run").json()
            assert r["placed"] == [] and r["skipped"][0]["why"] == "an option order already exists for this zone"
            assert c.put("/api/settings", json={"opt_auto_closed": "yes"}).status_code == 400
    finally:
        with SessionLocal() as db:
            _unset(db, "opt_auto", "opt_auto_min_score", "opt_auto_closed")
            _clear(db, key)


def test_stocks_without_options_are_not_shown_or_traded(monkeypatch):
    key_opt, key_plain = "NSE_EQ|OPT8", "NSE_EQ|PLAIN8"
    setup(key_opt, "OPH")
    with SessionLocal() as db:
        seed(db, key_plain)
        build(db, key_plain)                                   # a signal, but no option contracts
        assert optionpaper.option_stocks(db, {key_opt, key_plain}, NOW.date()) == {key_opt}
        assert optionpaper.option_stocks(db, {key_opt}, date(2026, 7, 1)) == set()      # all expired by then
    monkeypatch.setattr("app.main.data_sync.now_ist", lambda: NOW)
    with TestClient(app) as c:
        keys = {s["instrument_key"] for s in c.get("/api/option-paper/positions").json()["signals"]}
        assert key_opt in keys and key_plain not in keys
    real = live.watch_rows
    monkeypatch.setattr(live, "watch_rows", lambda db: [(z, p) for z, p in real(db) if z.instrument_key in (key_opt, key_plain)])
    optionpaper._tried.clear()
    with SessionLocal() as db:
        settings_store.put(db, "opt_auto_min_score", 7)
        out = asyncio.run(optionpaper.auto_place(db, FakeClient(530.0, "OPH"), live.watch_rows(db), NOW, force=True))
        _unset(db, "opt_auto_min_score")
        assert [p["label"] for p in out["placed"]] == ["OPH 25 JUN 530 CE"] and all("PLAIN8" not in x["symbol"] for x in out["skipped"])
        _clear(db, key_opt)


def test_options_shown_for_signals_are_in_the_price_round():
    optionpaper._previews.clear()
    optionpaper._previews[999001] = (NOW, {"option_key": "NSE_FO|SHOWN1", "strike": 530.0})
    optionpaper._previews[999002] = (NOW - timedelta(minutes=20), {"option_key": "NSE_FO|OLD1"})     # older than 15 min
    with SessionLocal() as db:
        keys = optionpaper.watch_keys(db, NOW)
    assert "NSE_FO|SHOWN1" in keys and "NSE_FO|OLD1" not in keys
    optionpaper._previews.clear()
