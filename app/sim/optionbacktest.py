"""Option backtest: the trades of a finished stock backtest, replayed with options.

Each stock trade is bought as an option on the entry and sold on the exit (or on the expiry day at 15:15 when the
option expires first), the way Option paper would have done it on that day: a call on a BUY, a put on a SELL, the
expiry and the strike by the mode's minimum days and target delta, 1 lot (or the lots of the settings).

Two sources for the past option prices:
  real      Upstox's expired-contract candles (an Upstox Plus feature). The strike's delta is worked out from the
            real at-the-money premium (its implied volatility) with Black-Scholes.
  estimate  Black-Scholes from the stock's own candles, with the stock's 20-day volatility as the IV and the expiry
            calendar NSE uses (Tuesdays from September 2025, Thursdays before; monthly for stocks, weekly for indices).
            A guide: no real spread, and IV spikes around news are missed.
"auto" uses real prices where Upstox gives them and the estimate otherwise; each trade says which it used."""
from __future__ import annotations

import asyncio
import math
from collections import Counter
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import settings_store
from ..db import SessionLocal
from types import SimpleNamespace

from ..models import (BacktestRun, BacktestTrade, Candle, Instrument, OptionBacktestRun, OptionBacktestTrade)
from ..upstox.client import UpstoxError
from . import backtest as bt
from . import optionpaper

SOURCES = ("auto", "real", "estimate")
RATE = 0.065                  # yearly risk-free rate for Black-Scholes
EXPIRY_TIME = time(15, 30)
CLOSE_TIME = time(15, 15)     # an option expiring before the stock's exit is sold on its expiry day at this time
TUESDAY_FROM = date(2025, 9, 1)
CANCEL: set[int] = set()


# ---------------------------------------------------------------- Black-Scholes
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def years_left(ts: datetime, expiry: str) -> float:
    end = datetime.combine(date.fromisoformat(expiry), EXPIRY_TIME)
    return max((end - ts).total_seconds(), 600) / (365 * 86400)


def bs_price(spot: float, strike: float, t: float, iv: float, kind: str) -> float:
    if iv <= 0 or t <= 0:
        return max(spot - strike, 0) if kind == "CE" else max(strike - spot, 0)
    d1 = (math.log(spot / strike) + (RATE + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "CE":
        return spot * _ncdf(d1) - strike * math.exp(-RATE * t) * _ncdf(d2)
    return strike * math.exp(-RATE * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot: float, strike: float, t: float, iv: float, kind: str) -> float:
    d1 = (math.log(spot / strike) + (RATE + iv * iv / 2) * t) / (iv * math.sqrt(t))
    return _ncdf(d1) if kind == "CE" else _ncdf(d1) - 1


def implied_vol(price: float, spot: float, strike: float, t: float, kind: str) -> float | None:
    """The volatility that gives this premium (bisection); None when the premium is below its intrinsic value."""
    lo, hi = 0.01, 3.0
    if price <= 0 or bs_price(spot, strike, t, hi, kind) < price or bs_price(spot, strike, t, lo, kind) > price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2, 4)


# ---------------------------------------------------------------- the stock's own data
def stock_price(db: Session, key: str, ts: datetime) -> float | None:
    """The stock's last close at or before ts, from the finest stored candles."""
    for tf in ("5m", "15m", "30m", "1h", "1d"):
        c = db.scalars(select(Candle).where(Candle.instrument_key == key, Candle.timeframe == tf, Candle.ts <= ts)
                       .order_by(Candle.ts.desc()).limit(1)).first()
        if c is not None and (ts - c.ts) < timedelta(days=5):
            return float(c.close)
    return None


def hist_vol(db: Session, key: str, day: date, n: int = 20) -> float:
    """The stock's yearly volatility from its last n daily closes before `day` (at least 10%)."""
    rows = db.scalars(select(Candle.close).where(Candle.instrument_key == key, Candle.timeframe == "1d",
                                                 Candle.ts < datetime.combine(day, time())).order_by(Candle.ts.desc()).limit(n + 1)).all()
    closes = [float(c) for c in reversed(rows) if c]
    if len(closes) < 6:
        return 0.30
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1))
    return max(0.10, round(sd * math.sqrt(252), 4))


def strike_step(db: Session, key: str, price: float) -> float:
    """The gap between strikes: from today's contracts of the stock, else NSE's usual steps by price."""
    strikes = sorted({float(s) for s in db.scalars(select(Instrument.strike).where(
        Instrument.underlying_key == key, Instrument.segment == "NSE_FO", Instrument.instrument_type == "CE")).all() if s})
    gaps = Counter(round(b - a, 2) for a, b in zip(strikes, strikes[1:]) if b > a)
    if gaps:
        return gaps.most_common(1)[0][0]
    for limit, step in ((250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50)):
        if price < limit:
            return step
    return 100.0


def lot_size_now(db: Session, key: str) -> int | None:
    return db.scalar(select(Instrument.lot_size).where(Instrument.underlying_key == key, Instrument.segment == "NSE_FO",
                                                       Instrument.instrument_type.in_(("CE", "PE")), Instrument.lot_size.isnot(None)).limit(1))


def calendar_expiries(start: date, end: date, weekly: bool) -> list[str]:
    """NSE's expiry days: Tuesday from 1 Sep 2025, Thursday before; every week for an index, the last of the month
    for a stock. Holidays are not moved (an estimate)."""
    out, d = [], start
    while d <= end:
        wd = 1 if d >= TUESDAY_FROM else 3
        if d.weekday() == wd:
            nxt = d + timedelta(days=7)
            if weekly or nxt.month != d.month:
                out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- real past option prices
class RealPrices:
    """Upstox's expired contracts, cached for the run. Any refusal (no Upstox Plus) turns `ok` off."""

    def __init__(self, client):
        self.client, self.ok, self.why = client, client is not None, None
        self._exp: dict[str, list[str]] = {}
        self._contracts: dict[tuple, list[dict]] = {}
        self._candles: dict[tuple, list[dict]] = {}

    async def _call(self, coro):
        try:
            return await coro
        except Exception as exc:                       # 401/403 without Upstox Plus, or no data: use the estimate
            text = str(exc)
            if isinstance(exc, UpstoxError) and any(w in text for w in ("401", "403", "not enabled", "subscription", "Plus", "Token", "token")):
                self.ok, self.why = False, f"Upstox refused the expired-contract data ({text[:80]})"
            raise

    async def expiries(self, db: Session, key: str, today: date) -> list[str]:
        if key not in self._exp:
            past = await self._call(self.client.expired_expiries(key))
            live = [str(d)[:10] for d in db.scalars(select(Instrument.expiry).where(
                Instrument.underlying_key == key, Instrument.segment == "NSE_FO", Instrument.expiry >= datetime.combine(today, time()))).all() if d]
            self._exp[key] = sorted(set(past) | set(live))
        return self._exp[key]

    async def contracts(self, db: Session, key: str, expiry: str, today: date) -> list[dict]:
        k = (key, expiry)
        if k not in self._contracts:
            if date.fromisoformat(expiry) >= today:       # not expired yet: today's instrument file has it
                rows = db.scalars(select(Instrument).where(Instrument.underlying_key == key, Instrument.segment == "NSE_FO",
                                                           Instrument.instrument_type.in_(("CE", "PE")))).all()
                self._contracts[k] = [{"instrument_key": r.instrument_key, "strike_price": r.strike, "instrument_type": r.instrument_type,
                                       "lot_size": r.lot_size, "live": True} for r in rows if r.expiry and r.expiry.date().isoformat() == expiry]
            else:
                self._contracts[k] = await self._call(self.client.expired_option_contracts(key, expiry))
        return self._contracts[k]

    async def candles(self, c: dict, start: date, end: date) -> list[dict]:
        interval = "1minute" if (end - start).days <= 5 else "30minute"
        k = (c["instrument_key"], start, end)
        if k not in self._candles:
            if c.get("live"):
                unit, n = ("minutes", 1) if interval == "1minute" else ("minutes", 30)
                self._candles[k] = sorted(await self._call(self.client.historical(c["instrument_key"], unit, n, end, start)), key=lambda x: x["ts"])
            else:
                self._candles[k] = await self._call(self.client.expired_candles(c["instrument_key"], interval, end, start))
        return self._candles[k]


def price_at(candles: list[dict], ts: datetime) -> float | None:
    """The option's price at ts: the close of the last candle that started at or before it, else the open of the
    first one after it on the same day."""
    before = [c for c in candles if c["ts"] <= ts]
    if before and ts - before[-1]["ts"] < timedelta(hours=18):
        return float(before[-1]["close"])
    after = [c for c in candles if c["ts"] > ts and c["ts"].date() == ts.date()]
    return float(after[0]["open"]) if after else None


# ---------------------------------------------------------------- one trade
def _exit_point(t: BacktestTrade, expiry: str) -> tuple[datetime, str]:
    close_day = datetime.combine(date.fromisoformat(expiry), CLOSE_TIME)
    if t.exit_ts is None or close_day < t.exit_ts:
        return close_day, "expiry"
    return t.exit_ts, "stock exit"


async def replay(db: Session, t: BacktestTrade, opts: dict, real: RealPrices | None, today: date) -> dict:
    """One stock trade as an option trade. Returns the fields of an OptionBacktestTrade."""
    key = t.instrument_key
    signal = "BUY" if t.side == "demand" else "SELL"
    kind = "CE" if signal == "BUY" else "PE"
    base = {"stock_trade_id": t.id, "instrument_key": key, "mode": t.mode, "timeframe": t.timeframe, "signal": signal,
            "option_type": kind, "entry_ts": t.entry_ts, "stock_entry": t.entry, "stock_stop": t.stop, "stock_r": t.result_r}
    if t.exit_ts is None:
        return {**base, "status": "skipped", "note": "the stock trade was still open at the end of the backtest"}
    min_days, want = opts["min_days"].get(t.mode, 7), opts["delta"].get(t.mode, optionpaper.TARGET_DELTA)
    day = t.entry_ts.date()
    if real is not None and real.ok and opts["source"] in ("auto", "real"):
        try:
            out = await _replay_real(db, t, base, kind, want, min_days, opts, real, today)
        except Exception as exc:                       # no data from Upstox for this trade
            out = {**base, "status": "skipped", "note": f"real prices: {type(exc).__name__}: {str(exc)[:120]}", "_fallback": True}
        if not out.pop("_fallback", False) or opts["source"] == "real":
            return out                                 # done, or skipped for a real reason (no trades in the strike)
    elif opts["source"] == "real":
        return {**base, "status": "skipped", "note": (real.why if real else None) or "real option prices are not available"}
    return _replay_estimate(db, t, base, kind, want, min_days, opts, day)


async def _replay_real(db, t, base, kind, want, min_days, opts, real: RealPrices, today: date) -> dict | None:
    exps = await real.expiries(db, t.instrument_key, today)
    expiry = optionpaper.pick_expiry([e for e in exps if e >= t.entry_ts.date().isoformat()], t.entry_ts.date(), min_days)
    if expiry is None:
        return {**base, "status": "skipped", "note": f"no expiry with at least {min_days} days after the entry", "_fallback": True}
    cons = [c for c in await real.contracts(db, t.instrument_key, expiry, today) if c.get("instrument_type") == kind and c.get("strike_price")]
    if not cons:
        return {**base, "status": "skipped", "note": f"no {kind} contracts for the {expiry} expiry", "_fallback": True}
    spot = float(t.entry)
    exit_ts, reason = _exit_point(t, expiry)
    start, end = t.entry_ts.date(), exit_ts.date()
    tl = years_left(t.entry_ts, expiry)
    atm = min(cons, key=lambda c: abs(float(c["strike_price"]) - spot))
    atm_px = price_at(await real.candles(atm, start, end), t.entry_ts)
    iv = implied_vol(atm_px, spot, float(atm["strike_price"]), tl, kind) if atm_px else None
    iv = iv or hist_vol(db, t.instrument_key, start)
    near = sorted(cons, key=lambda c: abs(float(c["strike_price"]) - spot))[:30]
    pick = min(near, key=lambda c: abs(abs(bs_delta(spot, float(c["strike_price"]), tl, iv, kind)) - want))
    cs = await real.candles(pick, start, end)
    p_in = price_at(cs, t.entry_ts)
    if not p_in:
        return {**base, "status": "skipped", "note": f"no trades in the {pick['strike_price']:g} {kind} at the entry time"}
    p_out = price_at(cs, exit_ts)
    if p_out is None:
        return {**base, "status": "skipped", "note": f"no price for the {pick['strike_price']:g} {kind} at the exit"}
    k = float(pick["strike_price"])
    return _finish(db, t, base, kind, expiry, k, int(pick.get("lot_size") or 0) or lot_size_now(db, t.instrument_key),
                   p_in, p_out, exit_ts, reason, iv, tl, opts, "real")


def _replay_estimate(db, t, base, kind, want, min_days, opts, day: date) -> dict:
    weekly = t.instrument_key.startswith("NSE_INDEX")
    exps = calendar_expiries(day, day + timedelta(days=400), weekly)
    expiry = optionpaper.pick_expiry(exps, day, min_days)
    if expiry is None:
        return {**base, "status": "skipped", "note": f"no expiry with at least {min_days} days after the entry"}
    lot = lot_size_now(db, t.instrument_key)
    if not lot:
        return {**base, "status": "skipped", "note": "no option contracts for this stock now (lot size unknown)"}
    spot = float(t.entry)
    iv = hist_vol(db, t.instrument_key, day)
    tl = years_left(t.entry_ts, expiry)
    step = strike_step(db, t.instrument_key, spot)
    centre = round(spot / step) * step
    grid = [centre + i * step for i in range(-15, 16) if centre + i * step > 0]
    k = min(grid, key=lambda s: abs(abs(bs_delta(spot, s, tl, iv, kind)) - want))
    exit_ts, reason = _exit_point(t, expiry)
    s_out = float(t.exit_price) if reason == "stock exit" and t.exit_price else stock_price(db, t.instrument_key, exit_ts)
    if s_out is None:
        return {**base, "status": "skipped", "note": "no stored stock price at the option's expiry"}
    p_in = bs_price(spot, k, tl, iv, kind)
    p_out = bs_price(s_out, k, years_left(exit_ts, expiry), iv, kind)
    return _finish(db, t, base, kind, expiry, k, lot, p_in, p_out, exit_ts, reason, iv, tl, opts, "estimate", s_out)


def _finish(db, t, base, kind, expiry, k, lot, p_in, p_out, exit_ts, reason, iv, tl, opts, source, s_out=None) -> dict:
    spot = float(t.entry)
    if s_out is None:
        s_out = float(t.exit_price) if reason == "stock exit" and t.exit_price else stock_price(db, t.instrument_key, exit_ts)
    p_in, p_out = round(max(p_in, 0.05), 2), round(max(p_out, 0.05), 2)
    at_stop = bs_price(float(t.stop), k, tl, iv, kind) * (p_in / max(bs_price(spot, k, tl, iv, kind), 0.05))   # scaled to the real fill
    risk_unit = max(p_in - at_stop, 0.05)
    lots = opts["fixed_lots"] if opts["lot_mode"] == "fixed" else int(opts["budget"] // (risk_unit * lot))
    symbol = db.scalar(select(Instrument.symbol).where(Instrument.instrument_key == t.instrument_key)) or t.instrument_key.split("|")[-1]
    label = f"{symbol} {date.fromisoformat(expiry):%d %b %y} {k:g} {kind}".upper()
    if lots <= 0:
        return {**base, "status": "skipped", "symbol": symbol, "option_label": label,
                "note": f"1 lot risks about ₹{risk_unit * lot:,.0f}, above the risk budget of ₹{opts['budget']:,.0f}"}
    qty = lots * lot
    pnl = round((p_out - p_in) * qty, 2)
    ch = optionpaper.charges(p_in * qty, p_out * qty, opts["th"])
    risk = round(risk_unit * qty, 2)
    return {**base, "status": "done", "symbol": symbol, "source": source, "option_label": label, "strike": k, "expiry": expiry,
            "delta": round(bs_delta(spot, k, tl, iv, kind), 3), "iv": round(iv, 4), "lot_size": lot, "lots": lots,
            "exit_ts": exit_ts, "stock_exit": round(s_out, 2) if s_out else None, "premium_in": p_in, "premium_out": p_out,
            "exit_reason": reason, "risk_amount": risk, "pnl": pnl, "charges": ch, "pnl_after": round(pnl - ch, 2),
            "result_r": round((pnl - ch) / risk, 3) if risk else None,
            "note": None if source == "real" else "estimated with Black-Scholes (20-day volatility)"}


# ---------------------------------------------------------------- the run
SCAN_ENTERED = ("target hit", "stopped", "trailed out", "open")


def is_scan(src: BacktestRun | None) -> bool:
    return src is not None and isinstance(src.settings, dict) and src.settings.get("kind") == "scan"


def source_trades(db: Session, src: BacktestRun | None) -> list:
    """The stock trades to replay: a date-range backtest's trades, or the entries of a one-date scan check (each
    entry type that was entered: its entry, stop, exit and R; one still open at the end is skipped later)."""
    if src is None:
        return []
    if not is_scan(src):
        return db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == src.id).order_by(BacktestTrade.entry_ts)).all()
    out = []
    for row in (src.summary or {}).get("rows", []):
        for plan, o in (row.get("outcomes") or {}).items():
            if o.get("status") not in SCAN_ENTERED or o.get("entry") is None or o.get("stop") is None or not o.get("entry_ts"):
                continue
            opened = o["status"] == "open"
            out.append(SimpleNamespace(
                id=None, instrument_key=row["instrument_key"], side="demand" if o["entry"] > o["stop"] else "supply",
                entry=float(o["entry"]), stop=float(o["stop"]), target=float(o.get("target") or o["entry"]),
                entry_ts=datetime.fromisoformat(o["entry_ts"]), exit_ts=None if opened or not o.get("exit_ts") else datetime.fromisoformat(o["exit_ts"]),
                exit_price=o.get("exit_price"), result_r=None if opened else o.get("result_r"),
                mode=row.get("mode") or src.settings.get("mode"), timeframe=row.get("ltf"), plan=plan))
    return sorted(out, key=lambda t: t.entry_ts)


async def run_after_stock_backtest(stock_run_id: int, option_run_id: int, client, today: date) -> None:
    """One button: the date-range stock backtest first (in a thread, its progress shown on the option run), then
    the option replay of its trades."""
    task = asyncio.ensure_future(asyncio.to_thread(bt.execute, stock_run_id))
    while not task.done():
        await asyncio.sleep(1)
        with SessionLocal() as db:
            src, run = db.get(BacktestRun, stock_run_id), db.get(OptionBacktestRun, option_run_id)
            if option_run_id in CANCEL:
                bt.CANCEL.add(stock_run_id)
            if src is not None and run is not None:
                run.progress = f"stock backtest: {src.progress or 'starting'}"
                db.commit()
    try:
        await task
    except Exception:                                   # the stock run records its own error
        pass
    with SessionLocal() as db:
        src, run = db.get(BacktestRun, stock_run_id), db.get(OptionBacktestRun, option_run_id)
        if src is None or src.status != "done":
            run.status = "cancelled" if option_run_id in CANCEL else "failed"
            run.error = None if run.status == "cancelled" else f"the stock backtest did not finish: {(src.error if src else None) or (src.status if src else 'missing')}"
            run.ended_at = datetime.now()
            db.commit()
            CANCEL.discard(option_run_id)
            return
        run.progress = "options: starting"
        db.commit()
    await execute(option_run_id, client, today)


def options_for(db: Session, source: str) -> dict:
    th = optionpaper._th(db)
    budget = float(settings_store.get(db, "opt_paper_capital")) * float(settings_store.get(db, "opt_paper_risk_pct")) / 100
    from ..config import MODES
    return {"source": source, "th": th, "budget": budget,
            "lot_mode": settings_store.get(db, "opt_lot_mode") if settings_store.get(db, "opt_lot_mode") in optionpaper.LOT_MODES else "fixed",
            "fixed_lots": int(settings_store.get(db, "opt_fixed_lots") or 1),
            "delta": {m: optionpaper.mode_delta(db, m) for m in MODES},
            "min_days": {m: optionpaper.min_days_for(db, m) for m in MODES}}


def summarize(rows: list[OptionBacktestTrade]) -> dict:
    done = [r for r in rows if r.status == "done"]
    wins = [r for r in done if (r.pnl_after or 0) > 0]
    gross_win = sum(r.pnl_after for r in wins)
    gross_loss = -sum(r.pnl_after for r in done if (r.pnl_after or 0) < 0)
    eq = peak = dd = 0.0
    for r in sorted(done, key=lambda r: r.exit_ts or r.entry_ts):
        eq += r.pnl_after or 0
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    rs = [r.result_r for r in done if r.result_r is not None]
    stock_rs = [r.stock_r for r in done if r.stock_r is not None]
    return {"trades": len(done), "skipped": len(rows) - len(done), "wins": len(wins),
            "win_rate": round(100 * len(wins) / len(done), 1) if done else None,
            "total_pnl": round(sum(r.pnl_after or 0 for r in done), 2), "charges": round(sum(r.charges or 0 for r in done), 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2) if rs else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None, "max_drawdown": round(dd, 2),
            "real": sum(1 for r in done if r.source == "real"), "estimate": sum(1 for r in done if r.source == "estimate"),
            "stock_total_r": round(sum(stock_rs), 2) if stock_rs else None,
            "stock_win_rate": round(100 * sum(1 for x in stock_rs if x > 0) / len(stock_rs), 1) if stock_rs else None,
            "by_exit": dict(Counter(r.exit_reason for r in done))}


async def execute(run_id: int, client, today: date | None = None) -> None:
    """Replay every trade of the stock run; progress and the summary are saved as it goes."""
    today = today or date.today()
    with SessionLocal() as db:
        run = db.get(OptionBacktestRun, run_id)
        opts = options_for(db, (run.settings or {}).get("source", "auto"))
        trades = source_trades(db, db.get(BacktestRun, run.source_run_id))
        real = RealPrices(client) if opts["source"] in ("auto", "real") else None
        try:
            for n, t in enumerate(trades, 1):
                if run_id in CANCEL:
                    run.status = "cancelled"
                    break
                fields = await replay(db, t, opts, real, today)
                db.add(OptionBacktestTrade(run_id=run_id, **fields))
                run.progress = f"{n} of {len(trades)} trades" + (f" · real prices off: {real.why}" if real is not None and real.why else "")
                db.commit()
                await asyncio.sleep(0)
            else:
                run.status = "done"
            rows = db.scalars(select(OptionBacktestTrade).where(OptionBacktestTrade.run_id == run_id)).all()
            run.summary = {**summarize(rows), "real_prices": None if real is None else (real.why or "used where Upstox had them")}
        except Exception as exc:
            run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"
        run.ended_at = datetime.now()
        db.commit()
        CANCEL.discard(run_id)
