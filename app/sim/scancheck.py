"""Scan-date check: what the GTF rules said about a stock on one date, using only the candles up to that date,
and what then happened to the entry its trade score takes, in the candles after it. No Upstox call is made.

A trade score of 7 or more takes entry type 1; a score of 5 to 6.5 takes entry types 2 and 3 (both are followed).
The entry has to come on the zone's next test: once that test is over the zone is no longer what was scored.

Trailing stop (Settings, off by default): the stop of an entered trade moves as sim/trailing.py says, on the closed
LTF or ITF candles after the fill, exactly as in paper trading. A trade that ends on a stop that has moved is
"trailed out"; it counts as a win when its result after costs is above 0 R."""
from datetime import date, datetime, time

import numpy as np
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import MODES
from ..data.resample import close_time
from ..data.sync import load_candles
from ..engine import gtf
from ..engine.contact import contacts
from ..engine.core import TimeframeEngine
from ..engine.result import no_side_reason, zone_card
from ..engine import window
from ..engine.score import ema_point
from . import fills, trailing

PLANS = ("type1", "type2", "type3")
ENTERED = ("target hit", "stopped", "trailed out", "open")


def is_win(o: dict) -> bool:
    return o["status"] == "target hit" or (o["status"] == "trailed out" and (o.get("result_r") or 0) > 0)


class TrailFeed:
    """Feeds a trade's trailing stop with the closed candles of the trailing timeframe (the LTF engine's own candles,
    or the ITF candles that have closed by the end of each LTF candle)."""

    def __init__(self, opts: dict, eng: TimeframeEngine, ltf: str, itf: str, i_df, cost_pct: float):
        self.opts, self.eng, self.ltf, self.itf = opts, eng, ltf, itf
        self.use_itf = opts["trail_tf"] == "itf" and i_df is not None and not i_df.empty
        self.cost_frac = 2 * float(cost_pct or 0) / 100.0
        if self.use_itf:
            self.i_ts = [t.to_pydatetime() for t in i_df["ts"]]
            self.i_close = [close_time(t, itf) for t in self.i_ts]
            self.i_bars = list(zip(i_df["high"].astype(float), i_df["low"].astype(float), i_df["close"].astype(float)))

    def make(self, side: str, entry: float, stop: float, target: float, fill_i: int) -> trailing.Trailer:
        fill_ts = self.eng.ts[fill_i]
        if self.use_itf:
            before = [b for t, ct, b in zip(self.i_ts, self.i_close, self.i_bars) if ct <= fill_ts][-20:]
        else:
            before = [(self.eng.h[k], self.eng.l[k], self.eng.c[k]) for k in range(max(0, fill_i - 20), fill_i)]
        tr = trailing.Trailer(side, entry, stop, self.opts, before, target, self.cost_frac)
        tr.next_k = 0
        if self.use_itf:      # the first ITF candle that opened at or after the fill
            while tr.next_k < len(self.i_ts) and self.i_ts[tr.next_k] < fill_ts:
                tr.next_k += 1
        return tr

    def feed(self, tr: trailing.Trailer, i: int) -> None:
        """LTF candle i has closed: move the stop."""
        if not self.use_itf:
            tr.close_bar(self.eng.h[i], self.eng.l[i], self.eng.c[i])
            return
        end = close_time(self.eng.ts[i], self.ltf)
        while tr.next_k < len(self.i_ts) and self.i_close[tr.next_k] <= end:
            tr.close_bar(*self.i_bars[tr.next_k])
            tr.next_k += 1


def _walk(eng: TimeframeEngine, start: int, side: str, entry: float, stop: float, target: float, cost_pct: float,
          feed: TrailFeed | None = None, fill_i: int | None = None) -> dict:
    """Follow an open position from candle `start` to its exit, with the fill rules the backtest uses.
    With a trailing stop, each candle is checked against the stop as it stood, then the stop moves at its close."""
    risk = abs(entry - stop)
    tr = feed.make(side, entry, stop, target, start if fill_i is None else fill_i) if feed else None
    for i in range(start, len(eng)):
        cur_stop, cur_target = (tr.stop, tr.live_target()) if tr else (stop, target)
        hit = fills.exit_after_entry(side, cur_stop, cur_target, eng.o[i], eng.h[i], eng.l[i])
        if hit:
            moved = tr is not None and tr.moves > 0
            out = {"status": "target hit" if hit[1] == "win" else "trailed out" if moved else "stopped",
                   "exit_price": float(hit[0]), "exit_ts": eng.ts[i].isoformat(),
                   "result_r": round(fills.result_r(side, entry, hit[0], risk, cost_pct), 2)}
            if tr:
                out.update(stop_moves=tr.moves, final_stop=round(tr.stop, 2), best_r=round(tr.best_r, 2), trail_note=tr.why)
            return out
        if tr:
            feed.feed(tr, i)
    out = {"status": "open", "exit_price": float(eng.c[-1]), "exit_ts": eng.ts[-1].isoformat(),
           "result_r": round(fills.result_r(side, entry, float(eng.c[-1]), risk, cost_pct), 2)}
    if tr:
        out.update(stop_moves=tr.moves, final_stop=round(tr.stop, 2), best_r=round(tr.best_r, 2), trail_note=tr.why)
    return out


def check(db: Session, key: str, mode: str, scan_date: date, cost_pct: float = 0.05, trail: dict | None = None) -> dict:
    """trail: the trailing stop options (sim/trailing.py); None reads them from the settings."""
    htf, itf, ltf = MODES[mode]
    th = settings_store.thresholds(db, ltf)
    trail = trailing.options(trail if trail is not None else {k: settings_store.get(db, k) for k in trailing.OPTION_KEYS})
    out = {"instrument_key": key, "mode": mode, "htf": htf, "itf": itf, "ltf": ltf, "scan_date": str(scan_date), "scan_ts": None, "cmp": None,
           "market_state": None, "curve": None, "htf_trend": None, "itf_trend": None, "itf_reason": None, "trade_side": "NONE",
           "demand_zone": None, "supply_zone": None, "ltf_zone": None, "zone_state": None, "contact_ltf": [], "contact_htf": [],
           "direction_rule": th.get("direction_rule") or "location",
           "signal": {"signal": "NO_TRADE", "score": 0.0, "zone_type": None, "reason": None, "entry": None, "stoploss": None, "target": None,
                      "primary_entry": None, "waiting": False, "rr": None, "type1": None, "type2": None, "type3": None},
           "outcomes": {p: {"status": "not taken", "note": "no trade on the scan date"} for p in PLANS}, "last_ts": None}
    df, h_df, i_df = load_candles(db, key, ltf), load_candles(db, key, htf), load_candles(db, key, itf)
    if df.empty or h_df.empty or i_df.empty:
        out["signal"]["reason"] = "Candles are missing for one of this mode's timeframes"
        return out
    eng = TimeframeEngine(df, ltf, th)
    day_end = datetime.combine(scan_date, time(23, 59, 59))
    seen = [n for n, t in enumerate(eng.ts) if close_time(t, ltf) <= day_end]
    if not seen:
        out["signal"]["reason"] = f"No {ltf} candle had closed by {scan_date}. Stored candles start {eng.ts[0].date()}."
        return out
    cut = seen[-1]                       # the last LTF candle closed on or before the scan date
    for i in range(cut + 1):
        eng.step(i)
    t_close = close_time(eng.ts[cut], ltf)
    h_eng = TimeframeEngine(h_df, htf, settings_store.thresholds(db, htf))
    hi = 0
    while hi < len(h_eng) and close_time(h_eng.ts[hi], htf) <= t_close:
        h_eng.step(hi)
        hi += 1
    # scanner.py's history windows, as of the scan date (all history when the zone_history setting says so)
    h_from, i_from, l_from = (window.start(mode, tf, scan_date, th) for tf in (htf, itf, ltf))
    i_ok = [close_time(t.to_pydatetime(), itf) <= t_close and (i_from is None or t.to_pydatetime() >= i_from) for t in i_df["ts"]]
    i_closes = i_df["close"].to_numpy(dtype=float)[i_ok]
    h_closes = [c for t, c in zip(h_eng.ts[:hi], h_eng.c[:hi]) if h_from is None or t >= h_from]
    price = float(eng.c[cut])
    ctx = gtf.context(window.keep(h_eng.active, h_from), np.array(h_closes, dtype=float), i_closes, price, th)
    up = gtf.next_htf(mode)
    if th.get("check_next_htf") and up:          # GTF PDF p.30: the fresh zones one timeframe above, as of the scan date
        u_df = load_candles(db, key, up)
        if not u_df.empty:
            th_up = settings_store.thresholds(db, up)
            u_eng = TimeframeEngine(u_df, up, th_up)
            ui = 0
            while ui < len(u_eng) and close_time(u_eng.ts[ui], up) <= t_close:
                u_eng.step(ui)
                ui += 1
            ctx.update(above=gtf.usable_htf(u_eng.active, th_up), above_tf=up)
    th_htf = settings_store.thresholds(db, htf)
    out.update(scan_ts=eng.ts[cut].isoformat(), cmp=price, last_ts=eng.ts[-1].isoformat(), market_state=ctx["market_state"], curve=ctx["curve"],
               htf_trend=ctx["htf_trend"], itf_trend=ctx["itf_trend"], itf_reason=ctx["itf_reason"], trade_side=ctx["side"],
               demand_zone=zone_card(ctx["demand"], th_htf), supply_zone=zone_card(ctx["supply"], th_htf))
    if ctx["side"] == "NONE":
        out["signal"]["reason"] = no_side_reason(ctx)
        return out
    emas = eng.emas(cut)
    skipped: list = []
    found = gtf.candidates(window.keep(eng.active, l_from), ctx, price, emas, th, skipped)
    if not found:
        out["signal"]["reason"] = (f"The LTF zone is not traded: {skipped[0][1]}" if skipped
                                   else "No valid LTF zone between the price and the HTF zone")
        return out
    z, total, label = found[0]
    made_at = eng.ts.index(z.created_ts)
    state = gtf.zone_state(z, cut - made_at, float(eng.l[cut]), float(eng.h[cut]), price, th)
    plans = gtf.entry_plans(z, th)
    out["ltf_zone"], out["zone_state"] = zone_card(z, th, total, label), state
    out["ltf_zone"]["ema"] = ema_point(z, emas, th)
    out["contact_ltf"] = contacts(z, h_eng.active, price, th)
    if ctx["htf_zone"] is not None:
        out["contact_htf"] = contacts(ctx["htf_zone"], [], price, th, above_tf=htf)
    sig = out["signal"]
    sig.update(score=round(total, 2), zone_type=label, type1=plans["type1"], type2=plans["type2"], type3=plans["type3"])
    lead = gtf.primary_plan(plans, total, th)
    if lead is None:
        sig["reason"] = "The trade score is below 5"
        return out
    side, sign = z.side, 1 if z.side == "demand" else -1
    rr, stop = float(th["target_rr"]), float(z.distal)
    sig.update(signal=ctx["side"], primary_entry=lead["entry_type"], entry=lead["entry"], stoploss=lead["stoploss"], target=lead["target"],
               rr=lead["rr"], waiting=not lead["triggered"], watch=round(float(z.proximal), 2))
    res = out["outcomes"]
    by_score = "type 1" if total >= 7 else "types 2 and 3"
    testing_on_scan = bool(z.in_visit)

    feed = TrailFeed(trail, eng, ltf, itf, i_df, cost_pct) if trailing.is_on(trail) else None

    def taken(entry: float, i: int, start: int) -> dict:
        """An entry at candle i; the exit is looked for from candle `start` on."""
        target = entry + sign * rr * abs(entry - stop)
        return {"entry": float(entry), "entry_ts": eng.ts[i].isoformat(), "stop": stop, "target": float(target),
                **_walk(eng, start, side, float(entry), stop, float(target), cost_pct, feed, i)}

    if total >= 7:
        for p in ("type2", "type3"):
            res[p] = {"status": "not taken", "note": f"a trade score of {total:g} takes entry type 1"}
        res["type1"] = {"status": "never entered", "note": "price did not come back to the zone",
                        "entry": float(z.proximal), "stop": stop, "target": float(z.proximal + sign * rr * abs(z.proximal - stop))}
        if testing_on_scan:
            res["type1"] = {"status": "not taken", "note": "price was already at the zone on the scan date, so the limit had been filled before it"}
    else:
        res["type1"] = {"status": "not taken", "note": f"a trade score of {total:g} takes entry type 2 or 3"}
        for p, done in (("type2", z.t2_entry), ("type3", z.t3_entry)):
            res[p] = ({"status": "not taken", "note": "this entry had already happened before the scan date"} if testing_on_scan and done is not None
                      else {"status": "never entered", "note": "price did not come back to the zone"})

    # Walk forward. The zone was scored for its next test (or the one going on), so the entry must come within that test.
    open_plans = {p for p in PLANS if res[p]["status"] == "never entered"}
    test_seen = testing_on_scan
    for i in range(cut + 1, len(eng)):
        if not open_plans:
            break
        kinds = {e["type"] for e in eng.step(i) if e["zone"] is z}
        if "touched" in kinds:
            if test_seen:                 # a later test: not the opportunity that was scored
                break
            test_seen = True
            if "type1" in open_plans:     # the limit at the proximal line is filled on this candle
                entry = float(z.proximal)
                if fills.stop_hit(side, stop, eng.h[i], eng.l[i]):       # entry and stop loss in one candle counts as a loss
                    res["type1"] = {"entry": entry, "entry_ts": eng.ts[i].isoformat(), "stop": stop, "target": float(entry + sign * rr * abs(entry - stop)),
                                    "status": "stopped", "exit_price": stop, "exit_ts": eng.ts[i].isoformat(),
                                    "result_r": round(fills.result_r(side, entry, stop, abs(entry - stop), cost_pct), 2)}
                else:
                    res["type1"] = taken(entry, i, i + 1)
                open_plans.discard("type1")
        if "entry2" in kinds and "type2" in open_plans:
            if abs(z.t2_entry - stop) > 0:
                res["type2"] = taken(z.t2_entry, i, i)          # entered at this candle's open: the candle itself can already exit it
            else:
                res["type2"] = {"status": "never entered", "note": "the candle opened on the stop loss line"}
            open_plans.discard("type2")
        if "entry3" in kinds and "type3" in open_plans:
            res["type3"] = taken(z.t3_entry, i, i + 1)          # entered at this candle's close
            open_plans.discard("type3")
        over = "violated" in kinds or "consumed" in kinds or (test_seen and not z.in_visit)
        if over:
            why = ("the zone was violated before the entry came" if "violated" in kinds
                   else "price tested the zone and left without the candles this entry needs")
            for p in open_plans:
                res[p] = {"status": "never entered", "note": why}
            break
    return out


def check_key(key: str, mode: str, scan_date: date, cost_pct: float = 0.05, trail: dict | None = None) -> dict:
    """check() with its own database session, so it can run in a worker process (jobs/scanpool.py)."""
    from ..db import SessionLocal
    with SessionLocal() as db:
        return check(db, key, mode, scan_date, cost_pct, trail)


def summarize(rows: list[dict]) -> dict:
    out = {}
    for p in PLANS:
        got = [r["outcomes"][p] for r in rows]
        entered = [o for o in got if o["status"] in ENTERED]
        closed = [o for o in entered if o["status"] != "open"]
        wins = [o for o in closed if is_win(o)]
        out[p] = {"entered": len(entered), "target_hit": len(wins), "stopped": len(closed) - len(wins),
                  "open": len(entered) - len(closed), "never_entered": sum(1 for o in got if o["status"] == "never entered"),
                  "not_taken": sum(1 for o in got if o["status"] == "not taken"),
                  "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
                  "total_r": round(sum(o["result_r"] for o in closed), 2)}
    return out
