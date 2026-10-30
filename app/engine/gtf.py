"""GTF top-down logic: HTF zones give the location and the curve, the ITF gives the trend, and the LTF
gives the execution zone. Pure functions on zone-like objects and arrays, shared by the live scan and the backtest."""
import numpy as np

from .lifecycle import ACTIVE, counted_tests
from .score import entry_kind, htf_score, ltf_score


def nearest_zones(zones: list, cmp: float, th: dict):
    """Nearest HTF demand below price and supply above it. Price inside a zone still counts
    (demand while price is above its distal line, supply while price is below its distal line).
    With htf_zone_pick "fresh" (the default, GTF PDF page 31) only zones that price has never come back to count:
    a zone with any test is skipped and the next untested one further away is used. A candle that is still forming
    has not been counted as a test yet, so a zone stays usable during the HTF candle that first reaches it."""
    floor = float(th.get("min_htf_score") or 0)
    fresh_only = (th.get("htf_zone_pick") or "fresh") == "fresh"
    ok = [z for z in zones if z.status in ACTIVE and htf_score(z, th)[0] >= floor and not (fresh_only and int(z.touches or 0) > 0)]
    demand = [z for z in ok if z.side == "demand" and cmp > z.distal]
    supply = [z for z in ok if z.side == "supply" and cmp < z.distal]
    return (max(demand, key=lambda z: z.proximal) if demand else None,
            min(supply, key=lambda z: z.proximal) if supply else None)


def curve(cmp: float, nd, ns) -> str:
    """Where price sits between the nearest HTF demand and supply, in five bands."""
    if not nd or not ns or ns.proximal <= nd.proximal:
        return "UNKNOWN"
    d, s = nd.proximal, ns.proximal
    third = (s - d) / 3
    if cmp <= d:
        return "VERY LOW"
    if cmp <= d + third:
        return "LOW"
    if cmp <= d + 2 * third:
        return "EQUILIBRIUM"
    return "HIGH" if cmp < s else "VERY HIGH"


def htf_trend(closes: np.ndarray, nd, ns) -> str:
    """Zone breaches in the last 20 HTF closes: two or more above the supply is an uptrend,
    two or more below the demand is a downtrend."""
    last = closes[-20:]
    if ns is not None and int(np.sum(last > ns.proximal)) >= 2:
        return "UPTREND"
    if nd is not None and int(np.sum(last < nd.proximal)) >= 2:
        return "DOWNTREND"
    return "SIDEWAYS"


def itf_trend(closes: np.ndarray) -> dict:
    """Slope of the 50 SMA over 7 bars. With fewer bars a shorter SMA is used, and with under 20 the raw 7-bar slope."""
    n = len(closes)
    if n < 7:
        return {"trend": "SIDEWAYS", "reason": f"only {n} candles (7 needed)"}
    if n >= 57:
        sma = np.convolve(closes[-57:], np.ones(50) / 50, mode="valid")
        slope, limit, why = float(sma[-1] - sma[0]), float(closes[-1]) * 0.0005, "50 SMA slope over 7 bars"
    elif n >= 20:
        period = min(n - 7, 50)
        sma = np.convolve(closes[-(period + 7):], np.ones(period) / period, mode="valid")
        slope, limit, why = float(sma[-1] - sma[0]), float(closes[-1]) * 0.005, f"{period} SMA slope over 7 bars ({n} candles)"
    else:
        slope, limit, why = float(closes[-1] - closes[-7]), float(closes[-1]) * 0.02, f"price slope over 7 bars ({n} candles)"
    return {"trend": "UP" if slope > limit else "DOWN" if slope < -limit else "SIDEWAYS", "reason": f"{why} = {slope:.4f}"}


def direction_filter(band: str, itf: str, rule: str = "location") -> str:
    """Curve and ITF trend to a trade side.

    location (the default): location beats trend. At or near the HTF demand (very low, low) is always a buy and at or
    near the HTF supply (high, very high) is always a sell, whatever the ITF says. Only in equilibrium does the ITF
    decide: up buys, down sells, sideways is no trade.

    trend (the earlier rule): the ITF decides. Up buys unless price is already at or above supply; down sells unless
    it is already at or below demand; sideways is no trade."""
    if rule == "trend":
        if itf == "UP" and band in ("VERY LOW", "LOW", "EQUILIBRIUM", "HIGH"):
            return "BUY"
        if itf == "DOWN" and band in ("VERY HIGH", "HIGH", "EQUILIBRIUM", "LOW"):
            return "SELL"
        return "NONE"
    if band in ("VERY LOW", "LOW"):
        return "BUY"
    if band in ("HIGH", "VERY HIGH"):
        return "SELL"
    if band == "EQUILIBRIUM":
        return "BUY" if itf == "UP" else "SELL" if itf == "DOWN" else "NONE"
    return "NONE"


def context(htf_zones: list, htf_closes: np.ndarray, itf_closes: np.ndarray, cmp: float, th: dict) -> dict:
    """Everything the HTF and ITF say at this price: market state, curve, both trends and the trade side."""
    nd, ns = nearest_zones(htf_zones, cmp, th)
    band = curve(cmp, nd, ns)
    rule = th.get("direction_rule") or "location"
    h_trend = htf_trend(htf_closes, nd, ns) if len(htf_closes) else "SIDEWAYS"
    itf = itf_trend(itf_closes)
    if nd and ns:
        state, side = "NORMAL", direction_filter(band, itf["trend"], rule)
    elif nd:       # every supply above has been used up: price is expanding higher
        state, band = "BULLISH_EXPANSION", "EQUILIBRIUM"
        side = "BUY" if h_trend in ("UPTREND", "SIDEWAYS") and itf["trend"] == "UP" else "NONE"
    elif ns:
        state, band = "BEARISH_EXPANSION", "EQUILIBRIUM"
        side = "SELL" if h_trend in ("DOWNTREND", "SIDEWAYS") and itf["trend"] == "DOWN" else "NONE"
    else:
        state, side = "NO_ZONES", "NONE"
    return {"market_state": state, "curve": band, "htf_trend": h_trend, "itf_trend": itf["trend"], "itf_reason": itf["reason"],
            "rule": rule, "side": side, "demand": nd, "supply": ns, "htf_zone": nd if side == "BUY" else ns if side == "SELL" else None}


def ltf_ok(z) -> bool:
    """An execution zone needs an explosive leg-in and a reversal pattern (drop-base-rally or rally-base-drop)."""
    return bool(z.legin_explosive) and z.pattern in ("DBR", "RBD")


def candidates(ltf_zones: list, ctx: dict, cmp: float, ema, th: dict) -> list[tuple]:
    """Execution zones for the trade side, lying between price and the HTF zone. Returns (zone, score, label), best first."""
    hz, side = ctx["htf_zone"], ctx["side"]
    if hz is None or side == "NONE":
        return []
    buffer = abs(hz.proximal - hz.distal) * th["htf_buffer_pct"] / 100.0
    lo, hi = (hz.distal - buffer, cmp) if side == "BUY" else (cmp, hz.distal + buffer)
    want = "demand" if side == "BUY" else "supply"
    out = []
    for z in ltf_zones:
        if z.side != want or z.status not in ACTIVE or not ltf_ok(z):
            continue
        # Between the HTF zone and the price. A zone the price is inside right now still counts: that is "touching now".
        placed = (lo <= z.proximal and z.distal <= cmp) if side == "BUY" else (z.proximal <= hi and z.distal >= cmp)
        if not placed:
            continue
        if abs(cmp - z.proximal) / cmp * 100 > th["ltf_max_gap_pct"]:
            continue
        total, label = ltf_score(z, ema, th)
        if total > 0:
            out.append((z, total, label))
    return sorted(out, key=lambda r: -r[1])


def zone_state(z, candles_since: int, low: float, high: float, close: float, th: dict) -> str:
    demand = z.side == "demand"
    touching = (low <= z.proximal and close >= z.distal) if demand else (high >= z.proximal and close <= z.distal)
    if touching:
        return "TOUCHING_NOW"
    if candles_since <= 1:
        return "JUST_FORMED"
    reach = th["approach_pct"] / 100.0
    near = (z.proximal < close <= z.proximal * (1 + reach)) if demand else (z.proximal * (1 - reach) <= close < z.proximal)
    return "APPROACHING" if near else "ACTIVE"


def entry_plans(z, th: dict, touching: bool = False) -> dict:
    """The three GTF entry types as they stand for one execution zone, for the test that is going on or the next one.

    type 1: a limit at the proximal line, set and forget. Filled as soon as price comes back to the zone.
    type 2: one candle closes inside the zone and the next opens inside it. Entry at that open.
    type 3: one candle closes inside the zone and the next closes back beyond the proximal line. Entry at that close.
    The stop loss is the distal line and the target is `target_rr` times entry to stop loss, for all three."""
    sign = 1 if z.side == "demand" else -1
    rr, stop = float(th["target_rr"]), float(z.distal)
    testing, armed = bool(z.in_visit), bool(z.inside_armed)

    def plan(kind: str, entry: float | None, triggered: bool, note: str, ts=None) -> dict:
        target = None if entry is None else round(entry + sign * rr * abs(entry - stop), 2)
        return {"entry_type": kind, "entry": None if entry is None else round(entry, 2), "stoploss": round(stop, 2), "target": target,
                "rr": rr, "triggered": triggered, "note": note, "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts}

    hit = testing or touching
    one = plan("TYPE_1", float(z.proximal), hit, "Limit at the proximal line: price has reached the zone" if hit
               else "Limit at the proximal line: waiting for price to come back to the zone")
    waits = "A candle has closed inside the zone: " if armed else "Waiting for a candle to close inside the zone, then "
    two = (plan("TYPE_2", float(z.t2_entry), True, "A candle closed inside the zone and the next opened inside it: entry at that open", z.t2_ts)
           if testing and z.t2_entry is not None else plan("TYPE_2", None, False, waits + "the next candle must open inside it"))
    three = (plan("TYPE_3", float(z.t3_entry), True, "A candle closed inside the zone and the next closed back out of it: entry at that close", z.t3_ts)
             if testing and z.t3_entry is not None else plan("TYPE_3", None, False, waits + "the next candle must close back out of it"))
    return {"type1": one, "type2": two, "type3": three}


PLAN_KEY = {"TYPE_1": "type1", "TYPE_2": "type2", "TYPE_3": "type3"}


def primary_plan(plans: dict, total: float, th: dict) -> dict | None:
    """The entry the trade score takes, or None when the score is below 5."""
    kind = entry_kind(total, th)
    return plans[PLAN_KEY[kind]] if kind else None


def zone_brief(z, th: dict) -> dict | None:
    if z is None:
        return None
    total, label = htf_score(z, th)
    return {"zone_id": getattr(z, "id", None), "side": z.side, "proximal": z.proximal, "distal": z.distal, "pattern": z.pattern,
            "bases": z.base_count, "wick_tests": counted_tests(z, th), "testing": bool(z.in_visit), "has_gap": bool(z.has_gap), "score": total, "label": label}