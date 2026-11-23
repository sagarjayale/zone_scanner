"""Delta and minimum days by mode (Option paper), and the option backtest: Black-Scholes, NSE's expiry days, a stock
trade replayed with real past option prices (Upstox expired contracts) or with estimated ones, the run and its page."""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import settings_store
from app.db import SessionLocal
from app.main import app
from app.models import (BacktestRun, BacktestTrade, Candle, Instrument, OptionBacktestRun, OptionBacktestTrade, Setting)
from app.sim import optionbacktest as ob
from app.sim import optionpaper
from app.upstox.client import UpstoxError
from tests.test_z_option_paper import FakeClient, NOW, EXPIRY, setup, _clear


def _unset(db, *keys):
    for k in keys:
        row = db.get(Setting, k)
        if row is not None:
            db.delete(row)
    db.commit()


# ---------------------------------------------------------------- delta and days by mode
def test_delta_and_minimum_days_by_mode():
    with SessionLocal() as db:
        assert settings_store.get(db, "opt_strike_rule") == "mode"
        assert [optionpaper.mode_delta(db, m) for m in ("30M", "4HR", "DAILY", "WEEKLY", "MONTHLY", "YEARLY")] == [0.55, 0.6, 0.65, 0.7, 0.75, 0.75]
        assert [optionpaper.min_days_for(db, m) for m in ("HOURLY", "2HR", "DAILY", "WEEKLY", "QUARTERLY")] == [2, 7, 14, 30, 45]
        settings_store.put(db, "opt_mode_delta", {"DAILY": 0.6})
        settings_store.put(db, "opt_mode_min_days", {"DAILY": 10})
        assert optionpaper.mode_delta(db, "DAILY") == 0.6 and optionpaper.min_days_for(db, "DAILY") == 10
        settings_store.put(db, "opt_strike_rule", "delta")
        settings_store.put(db, "opt_days_rule", "one")
        assert optionpaper.mode_delta(db, "MONTHLY") == 0.55 and optionpaper.min_days_for(db, "MONTHLY") == 7
        _unset(db, "opt_mode_delta", "opt_mode_min_days", "opt_strike_rule", "opt_days_rule")
    from tests.test_z_option_paper import strikes
    s = strikes(530.0)                                       # call delta 0.5 at 530, 0.62 at 520, 0.75 at 510
    assert optionpaper.pick_strike(s, "BUY", "mode", 530.0, 550.0, 0.75)[0]["strike"] == 510.0
    assert optionpaper.pick_strike(s, "BUY", "mode", 530.0, 550.0, 0.62)[0]["strike"] == 520.0
    assert optionpaper.pick_strike(s, "BUY", "mode", 530.0, 550.0, 0.75)[2] == "delta nearest 0.75"


def test_the_mode_decides_the_strike_and_the_expiry_of_an_order():
    key, under = "NSE_EQ|OBT1", "OBA"
    plan_id = setup(key, under)                              # a DAILY plan; expiries 4 Jun (2 days) and 25 Jun (23 days)
    with SessionLocal() as db:
        from app.models import TradePlan
        plan = db.get(TradePlan, plan_id)
        t = asyncio.run(optionpaper.place(db, FakeClient(530.0, under), plan, NOW))["placed"]
        assert (t.expiry, t.strike) == (EXPIRY, 520.0)       # 14+ days, delta nearest 0.65 -> the 520 CE (0.62)
        assert t.note.startswith("Daily: delta nearest 0.65, at least 14 days")
        _clear(db, key)
        plan.mode = "MONTHLY"                                # 45+ days: no such expiry here
        got = asyncio.run(optionpaper.place(db, FakeClient(530.0, under), plan, NOW))
        assert got["skipped"] == "no expiry with at least 45 days left"
        db.rollback()
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"opt_mode_delta": {"DAILY": 1.2}}).status_code == 400
        assert c.put("/api/settings", json={"opt_mode_delta": {"MINUTE": 0.5}}).status_code == 400
        assert c.put("/api/settings", json={"opt_mode_min_days": {"DAILY": 2.5}}).status_code == 400
        assert c.put("/api/settings", json={"opt_days_rule": "x"}).status_code == 400
        assert c.put("/api/settings", json={"opt_mode_delta": {"DAILY": 0.6}, "opt_mode_min_days": {"DAILY": 10}, "opt_days_rule": "mode"}).status_code == 200
        t = c.get("/api/option-paper/positions").json()["mode_tables"]
        assert t["delta"]["DAILY"] == 0.6 and t["delta"]["MONTHLY"] == 0.75 and t["min_days"]["DAILY"] == 10
        page = c.get("/option-paper").text
        assert 'value="mode">Delta by mode' in page and 'id="mode-table"' in page and 'id="opt_days_rule"' in page
    with SessionLocal() as db:
        _unset(db, "opt_mode_delta", "opt_mode_min_days", "opt_days_rule")


# ---------------------------------------------------------------- the maths
def test_black_scholes_and_expiry_days():
    t = 30 / 365
    call, put = ob.bs_price(100, 100, t, 0.2, "CE"), ob.bs_price(100, 100, t, 0.2, "PE")
    assert call - put == pytest.approx(100 - 100 * __import__("math").exp(-ob.RATE * t), abs=1e-6)     # put-call parity
    assert 0.5 < ob.bs_delta(100, 100, t, 0.2, "CE") < 0.6 and -0.5 < ob.bs_delta(100, 100, t, 0.2, "PE") < -0.4
    assert ob.bs_delta(100, 80, t, 0.2, "CE") > 0.95 and ob.bs_delta(100, 130, t, 0.2, "CE") < 0.02
    assert ob.implied_vol(call, 100, 100, t, "CE") == pytest.approx(0.2, abs=1e-3)
    assert ob.implied_vol(0.0001, 100, 80, t, "CE") is None                     # below the intrinsic value
    # stocks: the last Tuesday of the month from September 2025, the last Thursday before
    assert ob.calendar_expiries(date(2025, 7, 1), date(2025, 10, 31), weekly=False) == ["2025-07-31", "2025-08-28", "2025-09-30", "2025-10-28"]
    assert ob.calendar_expiries(date(2026, 11, 1), date(2026, 11, 30), weekly=True) == ["2026-11-03", "2026-11-10", "2026-11-17", "2026-11-24"]
    cs = [{"ts": datetime(2026, 6, 2, 10, m), "open": 10 + m, "close": 10.5 + m} for m in (0, 1, 2)]
    assert ob.price_at(cs, datetime(2026, 6, 2, 10, 1, 30)) == 11.5 and ob.price_at(cs, datetime(2026, 6, 2, 9, 30)) == 10


# ---------------------------------------------------------------- one stock backtest, replayed
KEY, UNDER = "NSE_EQ|OBTX", "OBX"
ENTRY_TS, EXIT_TS = datetime(2026, 3, 10, 10, 30), datetime(2026, 3, 12, 14, 0)


def _stock_run(db, exit_ts=EXIT_TS):
    for model in (BacktestTrade, BacktestRun):
        pass
    run = BacktestRun(status="done", created_at=NOW, settings={"modes": ["DAILY"], "start": "2026-03-01", "end": "2026-03-31", "symbols": [KEY]},
                      summary={"trades": 2})
    db.add(run)
    db.commit()
    db.add(BacktestTrade(run_id=run.id, instrument_key=KEY, timeframe="30m", side="demand", score=8, entry=1000.0, stop=980.0, target=1040.0,
                         entry_ts=ENTRY_TS, exit_ts=exit_ts, exit_price=1012.0, result="win", result_r=0.6, mode="DAILY"))
    db.add(BacktestTrade(run_id=run.id, instrument_key=KEY, timeframe="30m", side="supply", score=7, entry=1000.0, stop=1020.0, target=960.0,
                         entry_ts=ENTRY_TS, exit_ts=None, exit_price=None, result="open", result_r=None, mode="DAILY"))
    if db.get(Instrument, KEY) is None:
        db.add(Instrument(instrument_key=KEY, symbol=UNDER, segment="NSE_EQ"))
    db.query(Instrument).filter(Instrument.underlying_key == KEY).delete()
    for k in range(900, 1110, 10):
        db.add(Instrument(instrument_key=f"NSE_FO|{UNDER}{k}", symbol=f"{UNDER} {k} CE", segment="NSE_FO", instrument_type="CE",
                          lot_size=250, expiry=datetime(2026, 12, 29), strike=float(k), underlying_key=KEY))
    db.query(Candle).filter(Candle.instrument_key == KEY).delete()
    for i in range(30):                                       # daily closes before the entry: about 1% moves a day
        db.add(Candle(instrument_key=KEY, timeframe="1d", ts=datetime(2026, 2, 1) + timedelta(days=i), open=1000, high=1010, low=990,
                      close=1000 * (1.01 if i % 2 else 0.99), volume=1, oi=0))
    db.commit()
    return run.id


def _clean(db, run_id):
    db.query(OptionBacktestTrade).delete()
    db.query(OptionBacktestRun).delete()
    db.query(BacktestTrade).filter(BacktestTrade.run_id == run_id).delete()
    db.query(BacktestRun).filter(BacktestRun.id == run_id).delete()
    db.commit()


def test_estimated_replay():
    with SessionLocal() as db:
        run_id = _stock_run(db)
        trade = db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run_id, BacktestTrade.exit_ts.isnot(None))).one()
        opts = ob.options_for(db, "estimate")
        r = asyncio.run(ob.replay(db, trade, opts, None, date(2026, 11, 17)))
        # Daily: 14+ days after 10 Mar 2026 -> the 31 Mar expiry (last Tuesday); delta nearest 0.65 on a 10-point grid
        assert (r["status"], r["source"], r["expiry"], r["option_type"], r["lot_size"], r["lots"]) == ("done", "estimate", "2026-03-31", "CE", 250, 1)
        assert r["strike"] < 1000 and 0.58 < r["delta"] < 0.72 and r["exit_reason"] == "stock exit" and r["stock_exit"] == 1012.0
        assert r["premium_out"] > r["premium_in"] and r["pnl"] == pytest.approx((r["premium_out"] - r["premium_in"]) * 250)
        assert r["pnl_after"] == pytest.approx(r["pnl"] - r["charges"]) and r["result_r"] > 0
        assert r["option_label"].startswith("OBX 31 MAR 26")
        open_one = db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run_id, BacktestTrade.exit_ts.is_(None))).one()
        assert asyncio.run(ob.replay(db, open_one, opts, None, date(2026, 11, 17)))["status"] == "skipped"
        # an exit after the expiry: sold on the expiry day at 15:15
        trade.exit_ts = datetime(2026, 4, 20, 10, 0)
        db.add(Candle(instrument_key=KEY, timeframe="30m", ts=datetime(2026, 3, 31, 15, 0), open=1005, high=1008, low=1003, close=1006, volume=1, oi=0))
        db.commit()
        r = asyncio.run(ob.replay(db, trade, opts, None, date(2026, 11, 17)))
        assert (r["exit_reason"], r["exit_ts"], r["stock_exit"]) == ("expiry", datetime(2026, 3, 31, 15, 15), 1006.0)
        _clean(db, run_id)


class PastClient:
    """Upstox's expired contracts for the test stock: expiries, contracts and 1-minute candles."""

    def __init__(self, refuse=False):
        self.refuse, self.calls = refuse, []

    async def expired_expiries(self, key):
        if self.refuse:
            raise UpstoxError("/v2/expired-instruments/expiries: 403 not enabled for your plan")
        return ["2026-03-17", "2026-03-31"]

    async def expired_option_contracts(self, key, expiry):
        return [{"instrument_key": f"NSE_FO|{k}|{expiry}", "strike_price": float(k), "instrument_type": "CE", "lot_size": 300}
                for k in range(950, 1060, 10)]

    async def expired_candles(self, ekey, interval, to_date, from_date):
        self.calls.append(ekey)
        k = float(ekey.split("|")[1])
        out = []
        for ts, spot in ((ENTRY_TS, 1000.0), (EXIT_TS, 1012.0)):
            px = round(ob.bs_price(spot, k, ob.years_left(ts, "2026-03-31"), 0.25, "CE"), 2)
            out.append({"ts": ts - timedelta(minutes=1), "open": px, "high": px, "low": px, "close": px, "volume": 10, "oi": 100})
        return out

    async def close(self):
        return None


def test_real_replay_and_fallback():
    with SessionLocal() as db:
        run_id = _stock_run(db)
        trade = db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run_id, BacktestTrade.exit_ts.isnot(None))).one()
        client = PastClient()
        real = ob.RealPrices(client)
        r = asyncio.run(ob.replay(db, trade, ob.options_for(db, "auto"), real, date(2026, 11, 17)))
        assert (r["status"], r["source"], r["expiry"], r["lot_size"]) == ("done", "real", "2026-03-31", 300)
        assert r["iv"] == pytest.approx(0.25, abs=0.01)                      # worked out from the real at-the-money premium
        assert r["strike"] < 1000 and r["premium_out"] > r["premium_in"] and len(client.calls) == 2
        # no Upstox Plus: auto falls back to the estimate, "real only" skips with the reason
        refused = ob.RealPrices(PastClient(refuse=True))
        r = asyncio.run(ob.replay(db, trade, ob.options_for(db, "auto"), refused, date(2026, 11, 17)))
        assert r["source"] == "estimate" and refused.ok is False and "403" in refused.why
        r = asyncio.run(ob.replay(db, trade, ob.options_for(db, "real"), refused, date(2026, 11, 17)))
        assert r["status"] == "skipped" and "403" in r["note"]
        _clean(db, run_id)


def test_option_backtest_run_and_page(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "client", PastClient())
    with SessionLocal() as db:
        run_id = _stock_run(db)
    with TestClient(app) as c:
        assert "Option backtest" in c.get("/option-backtest").text
        assert '"option-backtest", "Option backtest"' in c.get("/static/js/common.js").text
        d = c.get("/api/option-backtest/runs").json()
        assert any(r["id"] == run_id for r in d["stock_runs"])
        assert c.post("/api/option-backtest/run", json={"source_run_id": run_id, "source": "magic"}).status_code == 400
        assert c.post("/api/option-backtest/run", json={"source_run_id": 999999}).status_code == 404
        oid = c.post("/api/option-backtest/run", json={"source_run_id": run_id, "source": "auto"}).json()["run_id"]
        for _ in range(100):
            got = c.get(f"/api/option-backtest/{oid}").json()
            if got["status"] != "running":
                break
            __import__("time").sleep(0.05)
        assert got["status"] == "done", got
        s = got["summary"]
        assert (s["trades"], s["skipped"], s["real"], s["stock_total_r"]) == (1, 1, 1, 0.6) and s["total_pnl"] > 0
        assert {t["status"] for t in got["trades"]} == {"done", "skipped"}
        csv_text = c.get(f"/api/option-backtest/{oid}/export").text
        assert csv_text.startswith("stock_trade_id,instrument_key") and "real" in csv_text
        assert c.delete(f"/api/option-backtest/{oid}").json() == {"deleted": oid}
    with SessionLocal() as db:
        _clean(db, run_id)


def _scan_check(db):
    """A saved one-date check: one entry that went to its target, one still open, one never entered."""
    rows = [{"instrument_key": KEY, "mode": "DAILY", "ltf": "30m", "outcomes": {
        "type1": {"status": "target hit", "entry": 1000.0, "stop": 980.0, "target": 1040.0, "entry_ts": ENTRY_TS.isoformat(),
                  "exit_ts": EXIT_TS.isoformat(), "exit_price": 1012.0, "result_r": 0.6},
        "type2": {"status": "open", "entry": 1000.0, "stop": 1020.0, "target": 960.0, "entry_ts": ENTRY_TS.isoformat(),
                  "exit_ts": EXIT_TS.isoformat(), "exit_price": 1005.0, "result_r": -0.25},
        "type3": {"status": "never entered", "note": "price did not come back to the zone"}}}]
    run = BacktestRun(status="done", created_at=NOW, settings={"kind": "scan", "mode": "DAILY", "scan_date": "2026-03-09", "symbols": [KEY]},
                      summary={"summary": {"type1": {"entered": 1}, "type2": {"entered": 1}}, "rows": rows})
    db.add(run)
    db.commit()
    return run.id


def test_replay_a_scan_date_check(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "client", PastClient())
    with SessionLocal() as db:
        stock_id = _stock_run(db)                        # for the lot size, the strikes and the daily closes
        check_id = _scan_check(db)
        got = ob.source_trades(db, db.get(BacktestRun, check_id))
        assert [(t.plan, t.side, t.exit_ts is None) for t in got] == [("type1", "demand", False), ("type2", "supply", True)]
    with TestClient(app) as c:
        d = c.get("/api/option-backtest/runs").json()
        assert any(x["id"] == check_id and x["entered"] == 2 for x in d["scan_checks"]) and all(x["id"] != check_id for x in d["stock_runs"])
        oid = c.post("/api/option-backtest/run", json={"source_run_id": check_id, "source": "estimate"}).json()["run_id"]
        for _ in range(100):
            got = c.get(f"/api/option-backtest/{oid}").json()
            if got["status"] != "running":
                break
            __import__("time").sleep(0.05)
        assert got["status"] == "done" and got["settings"]["kind"] == "scan"
        assert (got["summary"]["trades"], got["summary"]["skipped"], got["summary"]["stock_total_r"]) == (1, 1, 0.6)
        js = c.get("/static/js/backtest.js").text
        assert "Replay with options" in js and "/api/option-backtest/run" in js
    with SessionLocal() as db:
        _clean(db, stock_id)
        db.query(BacktestRun).filter(BacktestRun.id == check_id).delete()
        db.commit()


def test_one_button_stock_backtest_then_options(monkeypatch):
    """The form: the date-range stock backtest runs first, then its trades are replayed with options."""
    import app.main as main
    from app.sim import backtest as bt
    monkeypatch.setattr(main, "client", PastClient())
    with SessionLocal() as db:
        template = _stock_run(db)                        # the instruments, candles and two trades to copy
        trades = [dict((c.name, getattr(t, c.name)) for c in BacktestTrade.__table__.columns if c.name not in ("id", "run_id"))
                  for t in db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == template))]

    def fake_execute(run_id):                            # stands in for the real stock backtest
        with SessionLocal() as db:
            for t in trades:
                db.add(BacktestTrade(run_id=run_id, **t))
            run = db.get(BacktestRun, run_id)
            run.status, run.progress, run.summary = "done", "done", {"trades": 2}
            db.commit()
    monkeypatch.setattr(bt, "execute", fake_execute)
    monkeypatch.setattr(main, "_keys", lambda db, symbols: [KEY])
    with SessionLocal() as db:
        have = {tf for (tf,) in db.execute(select(Candle.timeframe).where(Candle.instrument_key == KEY).distinct())}
        for tf in {"1d", "1h", "30m"} - have:            # _new_backtest checks the mode's candles are stored
            db.add(Candle(instrument_key=KEY, timeframe=tf, ts=datetime(2026, 1, 5), open=1, high=1, low=1, close=1, volume=1, oi=0))
        db.commit()
    with TestClient(app) as c:
        assert c.post("/api/option-backtest/start", json={"modes": ["DAILY"], "start": "2026-03-01", "end": "2026-03-31", "source": "x"}).status_code == 400
        assert c.post("/api/option-backtest/start", json={"modes": ["DAILY"], "start": "2026-03-31", "end": "2026-03-01"}).status_code == 400
        r = c.post("/api/option-backtest/start", json={"symbols": "all", "modes": ["DAILY"], "start": "2026-03-01", "end": "2026-03-31", "source": "auto"}).json()
        for _ in range(100):
            got = c.get(f"/api/option-backtest/{r['run_id']}").json()
            if got["status"] != "running":
                break
            __import__("time").sleep(0.1)
        assert got["status"] == "done", got
        assert got["source_run_id"] == r["stock_run_id"] and got["summary"]["trades"] == 1 and got["summary"]["real"] == 1
        page = c.get("/option-backtest").text
        assert 'id="n-modes"' in page and 'id="n-start"' in page and 'id="n-run"' in page and "Scan-date checks" in c.get("/static/js/optionbacktest.js").text
    with SessionLocal() as db:
        _clean(db, r["stock_run_id"])
        _clean(db, template)
