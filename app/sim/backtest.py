"""Backtest under GTF rules: the same engine and the same fill rules as live, run over stored candles.
No Upstox call is made. Each analysis mode is tested with its own HTF, ITF and LTF, and a candle on a
higher timeframe is only used once it has closed."""
from datetime import date, datetime, time

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import MODES
from ..data.resample import close_time
from ..data.sync import load_candles, now_ist
from ..db import SessionLocal
from ..engine import gtf
from ..engine.core import TimeframeEngine
from ..models import BacktestRun, BacktestTrade
from . import fills

CANCEL: set[int] = set()
ENTRY_PLANS = ("rule", "type1", "type2", "type3")


def run_one(db: Session, key: str, mode: str, start: datetime, end: datetime, opts: dict, cancelled=lambda: False) -> list[dict]:
    """Backtest one instrument in one analysis mode. Returns trades as dicts.

    type1: a limit at the proximal line of a zone whose trade score is 7 or more; filled when price comes back to it.
    type2: for a score of 5 to 6.5: a candle closes inside the zone and the next opens inside it; entry at that open.
    type3: for a score of 5 to 6.5: a candle closes inside the zone and the next closes back out; entry at that close.
    rule:  type 1 for 7 or more, and for 5 to 6.5 the one of types 2 and 3 chosen in the settings.
    A zone is scored at the close of the candle before the one that enters, so nothing looks ahead."""
    htf, itf, ltf = MODES[mode]
    th = settings_store.thresholds(db, ltf)
    min_score = float(opts["min_score"]) if opts.get("min_score") is not None else float(th["min_score"])
    cost_pct = float(opts.get("cost_pct", 0.05))
    entry_plan = opts.get("entry_plan") or "rule"
    two_three = "TYPE_2" if th.get("entry_23") == "TYPE_2" else "TYPE_3"
    want = {"rule": ("TYPE_1", two_three), "type1": ("TYPE_1",), "type2": ("TYPE_2",), "type3": ("TYPE_3",)}[entry_plan]
    rr = float(th["target_rr"])
    df = load_candles(db, key, ltf, until=end)
    h_df = load_candles(db, key, htf, until=end)
    i_df = load_candles(db, key, itf, until=end)
    if df.empty or h_df.empty or i_df.empty:
        return []
    eng = TimeframeEngine(df, ltf, th)
    h_eng = TimeframeEngine(h_df, htf, settings_store.thresholds(db, htf))
    h_close = [close_time(t, htf) for t in h_eng.ts]
    i_closes = i_df["close"].to_numpy(dtype=float)
    i_close = [close_time(t.to_pydatetime(), itf) for t in i_df["ts"]]
    hi = ii = 0
    pending: dict[int, dict] = {}        # type 1 limits that qualified at the previous close
    scored: dict[int, tuple] = {}        # zones scored 5 to 6.5 at the previous close, waiting for entry type 2 or 3
    open_trades: list[dict] = []
    done: list[dict] = []
    traded: set[int] = set()

    def finish(tr: dict, ts: datetime, price: float, result: str):
        best = tr.pop("best")
        if result == "win":
            best = max(best, tr["target"]) if tr["side"] == "demand" else min(best, tr["target"])
        tr["mfe_r"] = round(abs(best - tr["entry"]) / tr["risk"], 2)
        tr.update(exit_ts=ts, exit_price=float(price), result=result,
                  result_r=round(fills.result_r(tr["side"], tr["entry"], price, tr["risk"], cost_pct), 3))
        done.append(tr)

    def trade(z, total, label, ctx, entry, stop, target, kind, ts):
        return {"instrument_key": key, "timeframe": ltf, "mode": mode, "side": z.side, "score": total, "label": label,
                "entry": float(entry), "stop": float(stop), "target": float(target), "risk": abs(entry - stop),
                "reward_risk": round(abs(target - entry) / abs(entry - stop), 2), "pattern": z.pattern,
                "zone_created_ts": z.created_ts, "zone_proximal": float(z.proximal), "zone_distal": float(z.distal),
                "curve": ctx["curve"], "trend": ctx["itf_trend"], "entry_type": kind,
                "entry_ts": ts, "best": float(entry)}

    n = len(eng)
    for i in range(n):
        if i % 2000 == 0 and cancelled():
            return []
        ts, o, h, l, c = eng.ts[i], eng.o[i], eng.h[i], eng.l[i], eng.c[i]
        in_range = start <= ts <= end
        # 1. exits for trades opened on earlier candles
        for tr in list(open_trades):
            hit = fills.exit_after_entry(tr["side"], tr["stop"], tr["target"], o, h, l)
            if hit:
                open_trades.remove(tr)
                finish(tr, ts, hit[0], hit[1])
            else:
                tr["best"] = max(tr["best"], h) if tr["side"] == "demand" else min(tr["best"], l)
        # 2. type 1 limits that qualified at the previous close
        if in_range:
            for zid, od in list(pending.items()):
                if not fills.entry_touched(od["side"], od["entry"], h, l):
                    continue
                traded.add(zid)
                tr = dict(od, entry_ts=ts)
                if fills.stop_hit(od["side"], od["stop"], h, l):   # entry and stop in one candle counts as a loss
                    finish(tr, ts, od["stop"], "loss")
                else:
                    open_trades.append(tr)
        # 3. the engines see this candle only now that it has closed
        events = eng.step(i)
        # 3a. entry types 2 and 3 that this candle completed, for zones scored at the previous close
        if in_range:
            for e in events:
                kind = {"entry2": "TYPE_2", "entry3": "TYPE_3"}.get(e["type"])
                z = e["zone"]
                if kind is None or kind not in want or z.id not in scored or z.id in traded:
                    continue
                total, label, was = scored[z.id]
                entry, sign = float(e["price"]), 1 if z.side == "demand" else -1
                if abs(entry - z.distal) <= 0:
                    continue
                traded.add(z.id)
                tr = trade(z, total, label, was, entry, z.distal, entry + sign * rr * abs(entry - z.distal), kind, ts)
                hit = fills.exit_after_entry(z.side, tr["stop"], tr["target"], o, h, l) if kind == "TYPE_2" else None
                if hit:                    # entered at the open: this same candle can already reach the stop or the target
                    finish(tr, ts, hit[0], hit[1])
                else:
                    open_trades.append(tr)
        t_close = close_time(ts, ltf)
        while hi < len(h_eng) and h_close[hi] <= t_close:
            h_eng.step(hi)
            hi += 1
        while ii < len(i_close) and i_close[ii] <= t_close:
            ii += 1
        # 4. what the HTF and ITF say now, and which execution zones qualify for the next candle
        pending, scored = {}, {}
        last_bar = i + 1 >= n
        if last_bar or eng.ts[i + 1] < start:
            continue
        ctx = gtf.context(h_eng.active, h_eng.c[:hi], i_closes[:ii], float(c), th)
        if ctx["side"] == "NONE":
            continue
        for z, total, label in gtf.candidates(eng.active, ctx, float(c), eng.emas(i), th):
            if total < max(min_score, 5.0) or z.id in traded:
                continue
            if total >= 7:
                if "TYPE_1" in want and not z.in_visit and z.touches == 0:
                    sign = 1 if z.side == "demand" else -1
                    pending[z.id] = trade(z, total, label, ctx, z.proximal, z.distal, z.proximal + sign * rr * abs(z.proximal - z.distal), "TYPE_1", None)
            elif "TYPE_2" in want or "TYPE_3" in want:
                scored[z.id] = (total, label, ctx)
    for tr in open_trades:   # still open at the end of the data: valued at the last close, kept out of the statistics
        finish(tr, eng.ts[-1], float(eng.c[-1]), "open")
    return done


def summarize(trades: list[dict]) -> dict:
    closed = sorted((t for t in trades if t["result"] in ("win", "loss")), key=lambda t: (t["exit_ts"], t["entry_ts"]))

    def stats(rows: list[dict]) -> dict:
        if not rows:
            return {"trades": 0, "win_rate": None, "expectancy": None}
        wins = [t for t in rows if t["result"] == "win"]
        return {"trades": len(rows), "win_rate": round(100 * len(wins) / len(rows), 1),
                "expectancy": round(sum(t["result_r"] for t in rows) / len(rows), 3)}

    def by(field: str) -> dict:
        keys = sorted({t.get(field) for t in closed if t.get(field) is not None}, key=str)
        return {str(k): stats([t for t in closed if t.get(field) == k]) for k in keys}

    out = stats(closed)
    gain = sum(t["result_r"] for t in closed if t["result_r"] > 0)
    loss = -sum(t["result_r"] for t in closed if t["result_r"] < 0)
    equity, cum, peak, dd, streak, worst = [], 0.0, 0.0, 0.0, 0, 0
    for t in closed:
        cum += t["result_r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        streak = streak + 1 if t["result"] == "loss" else 0
        worst = max(worst, streak)
        equity.append([t["exit_ts"].isoformat(), round(cum, 3)])
    out.update(total_r=round(cum, 3), profit_factor=round(gain / loss, 2) if loss > 0 else None,
               max_drawdown_r=round(dd, 3), longest_losing_streak=worst,
               open_trades=sum(1 for t in trades if t["result"] == "open"), equity=equity,
               reached={str(n): sum(1 for t in closed if (t.get("mfe_r") or 0) >= n) for n in (1, 2, 3, 5, 10)},
               best_trade_r=max((t["result_r"] for t in closed), default=None),
               worst_trade_r=min((t["result_r"] for t in closed), default=None),
               by_score=by("score"), by_label=by("label"), by_mode=by("mode"), by_curve=by("curve"), by_timeframe=by("timeframe"))
    return out


def execute(run_id: int) -> None:
    """Runs in a worker thread with its own database session."""
    with SessionLocal() as db:
        run = db.get(BacktestRun, run_id)
        s = run.settings
        start = datetime.combine(date.fromisoformat(s["start"]), time.min)
        end = datetime.combine(date.fromisoformat(s["end"]), time.max).replace(microsecond=0)
        jobs = [(k, m) for k in s["symbols"] for m in s["modes"]]
        trades: list[dict] = []
        try:
            for n, (key, mode) in enumerate(jobs, 1):
                if run_id in CANCEL:
                    break
                run.progress = f"{n} of {len(jobs)}: {key} {mode}"
                db.commit()
                trades += run_one(db, key, mode, start, end, s, cancelled=lambda: run_id in CANCEL)
            if run_id in CANCEL:
                run.status = "cancelled"
            else:
                db.execute(delete(BacktestTrade).where(BacktestTrade.run_id == run_id))
                for t in trades:
                    db.add(BacktestTrade(run_id=run_id, **{k: v for k, v in t.items() if k != "risk"}))
                run.summary, run.status, run.progress = summarize(trades), "done", f"{len(trades)} trades"
        except Exception as exc:  # shown on the page
            db.rollback()
            run = db.get(BacktestRun, run_id)
            run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            CANCEL.discard(run_id)
            run.ended_at = now_ist()
            db.commit()
