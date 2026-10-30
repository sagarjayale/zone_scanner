"""Confirmation score: checks the zone rules do not make, kept apart from the scanner score.

From stored data (worked out at each scan, shown on the dashboard):
  mode agreement (2 points), HTF contact, index agreement, leg-out volume, relative strength (1 point each).
From the live option chain (worked out on the Options page):
  strike build-up, open-interest wall, PCR, IV level, time to expiry, premium reward to risk (1 point each).

A check that cannot be made (no index in the list, no volume, no options) is left out of both the points and the
maximum, so the score is always "points of what could be checked". 75% or more is strong, 50% or more moderate."""
from sqlalchemy.orm import Session

from ..config import MODES
from ..data.sync import load_candles
from ..models import ModeResult

INDEX_KEY = "NSE_INDEX|Nifty 50"
MODE_NAMES = {"30M": "30 min", "HOURLY": "Hourly", "2HR": "2 hr", "4HR": "4 hr", "DAILY": "Daily", "WEEKLY": "Weekly",
              "MONTHLY": "Monthly", "QUARTERLY": "Quarterly", "HALFYEARLY": "Half yearly", "YEARLY": "Yearly"}


def check(key: str, label: str, points: float, most: float, note: str, applicable: bool = True) -> dict:
    return {"key": key, "label": label, "points": points if applicable else 0, "max": most if applicable else 0,
            "applicable": applicable, "passed": applicable and points >= most, "note": note}


def summary(checks: list[dict]) -> dict:
    points, most = sum(c["points"] for c in checks), sum(c["max"] for c in checks)
    pct = round(100 * points / most) if most else None
    label = None if pct is None else "strong" if pct >= 75 else "moderate" if pct >= 50 else "weak"
    return {"points": points, "max": most, "pct": pct, "label": label, "checks": checks}


def agreement(side: str, higher: list[tuple[str, str]]) -> dict:
    """higher: (mode, signal) of every higher mode. 2 points when every higher mode that has a side agrees,
    1 when at least half do, 0 otherwise. Higher modes with no trade are not counted either way."""
    sided = [(m, s) for m, s in higher if s in ("BUY", "SELL")]
    if not sided:
        return check("modes", "Mode agreement", 0, 2, "no higher mode has a side to compare with", applicable=False)
    yes = [MODE_NAMES.get(m, m) for m, s in sided if s == side]
    no = [MODE_NAMES.get(m, m) for m, s in sided if s != side]
    points = 2 if not no else 1 if len(yes) * 2 >= len(sided) else 0
    note = (f"{', '.join(yes)} agree" if yes else "no higher mode agrees") + (f"; {', '.join(no)} against" if no else "")
    return check("modes", "Mode agreement", points, 2, note)


def scanner_checks(db: Session, key: str, mode: str, side: str, zone, contact_ltf: list[dict], th: dict) -> list[dict]:
    """The five checks that need only stored data."""
    ltf = MODES[mode][2]
    order = list(MODES)
    higher = [(m, r.signal) for m in order[order.index(mode) + 1:] if (r := db.get(ModeResult, (key, m))) is not None]
    out = [agreement(side, higher)]

    close = [c for c in contact_ltf if c["relation"] in ("inside", "overlap", "touch")]
    out.append(check("contact", "HTF contact", 1 if close else 0, 1,
                     f"{close[0]['relation']} with the {close[0]['tf']} zone" if close else "no higher zone touches the entry zone"))

    is_index = "_INDEX|" in key
    idx = None if is_index else db.get(ModeResult, (INDEX_KEY, mode))
    if is_index:
        out.append(check("index", "Index agreement", 0, 1, "this is an index", applicable=False))
    elif idx is None:
        out.append(check("index", "Index agreement", 0, 1, "Nifty 50 is not in your stock list", applicable=False))
    else:
        word = {"BUY": "buy", "SELL": "sell"}.get(idx.signal, "no trade")
        out.append(check("index", "Index agreement", 1 if idx.signal == side else 0, 1, f"Nifty 50 is {word} in this mode"))

    bars = load_candles(db, key, ltf, until=zone.created_ts, last=21)
    vol = bars["volume"].to_numpy(dtype=float) if not bars.empty else []
    if len(vol) < 6 or vol[:-1].mean() <= 0:
        out.append(check("volume", "Leg-out volume", 0, 1, "no volume history for this zone", applicable=False))
    else:
        ratio = float(vol[-1] / vol[:-1].mean())
        need = float(th.get("confirm_volume_mult") or 1.5)
        out.append(check("volume", "Leg-out volume", 1 if ratio >= need else 0, 1, f"{ratio:.1f} times the average of the candles before it (needs {need:g})"))

    mine = load_candles(db, key, ltf, last=21)
    theirs = None if is_index else load_candles(db, INDEX_KEY, ltf, last=21)
    if theirs is None or theirs.empty or len(mine) < 6 or len(theirs) < 6:
        out.append(check("strength", "Relative strength", 0, 1, "no Nifty 50 candles on this timeframe to compare with", applicable=False))
    else:
        a = 100 * (float(mine["close"].iloc[-1]) / float(mine["close"].iloc[0]) - 1)
        b = 100 * (float(theirs["close"].iloc[-1]) / float(theirs["close"].iloc[0]) - 1)
        ok = a > b if side == "BUY" else a < b
        out.append(check("strength", "Relative strength", 1 if ok else 0, 1, f"stock {a:+.1f}%, Nifty 50 {b:+.1f}% over the last {len(mine) - 1} candles"))
    return out


# ---------------------------------------------------------------- from the live option chain
def buildup(ltp: float | None, close: float | None, oi_change: float | None) -> str | None:
    """Price and open interest since the previous close: up and up is a long build-up, up and down short covering,
    down and up a short build-up, down and down long unwinding."""
    if not ltp or not close or oi_change is None or ltp == close:
        return None
    if ltp > close:
        return "long build-up" if oi_change > 0 else "short covering"
    return "short build-up" if oi_change > 0 else "long unwinding"


def premium_at(leg: dict, spot: float, price: float) -> float | None:
    """Estimated premium if the underlying were at `price`: delta for each point, plus the gamma correction.
    It ignores time decay and changes in IV, so it is only a guide."""
    if leg.get("delta") is None or not leg.get("ltp") or not spot:
        return None
    move = price - spot
    return round(max(leg["ltp"] + leg["delta"] * move + 0.5 * (leg.get("gamma") or 0) * move * move, 0.05), 2)


def option_checks(side: str, leg: dict | None, levels: dict | None, view: dict, iv: dict, days_left: int | None,
                  entry: float | None, target: float | None) -> list[dict]:
    out = []
    name = buildup(leg.get("ltp"), leg.get("close"), leg.get("oi_change")) if leg else None
    if name is None:
        out.append(check("buildup", "Strike build-up", 0, 1, "no price change since the previous close to judge", applicable=False))
    else:
        out.append(check("buildup", "Strike build-up", 1 if name in ("long build-up", "short covering") else 0, 1, f"{name} on the suggested option"))
    wall = view.get("call_wall") if side == "BUY" else view.get("put_wall")
    if wall is None or entry is None or target is None:
        out.append(check("wall", "Open-interest wall", 0, 1, "no open interest to judge", applicable=False))
    else:
        blocked = min(entry, target) < wall < max(entry, target)
        kind = "call" if side == "BUY" else "put"
        out.append(check("wall", "Open-interest wall", 0 if blocked else 1, 1,
                         f"largest {kind} open interest is at {wall:g}, " + ("between the entry and the target" if blocked else "not between the entry and the target")))
    pcr = view.get("pcr")
    if pcr is None:
        out.append(check("pcr", "Put-call ratio", 0, 1, "no open interest to judge", applicable=False))
    else:
        ok = pcr >= 1 if side == "BUY" else pcr <= 1
        out.append(check("pcr", "Put-call ratio", 1 if ok else 0, 1, f"{pcr} ({'1 or more' if side == 'BUY' else '1 or less'} supports a {'buy' if side == 'BUY' else 'sell'})"))
    if iv.get("percentile") is None:
        out.append(check("iv", "IV level", 0, 1, f"IV history still building, {iv.get('days', 0)} of {iv.get('needed', 20)} days", applicable=False))
    else:
        out.append(check("iv", "IV level", 1 if iv["percentile"] < 70 else 0, 1, f"IV percentile {iv['percentile']} (under 70 is not expensive)"))
    if days_left is None:
        out.append(check("expiry", "Time to expiry", 0, 1, "no expiry chosen", applicable=False))
    else:
        out.append(check("expiry", "Time to expiry", 1 if days_left >= 5 else 0, 1, f"{days_left} {'day' if days_left == 1 else 'days'} left (needs 5)"))
    if not levels or levels.get("rr") is None:
        out.append(check("premium_rr", "Premium reward to risk", 0, 1, "no premium estimate for this option", applicable=False))
    else:
        out.append(check("premium_rr", "Premium reward to risk", 1 if levels["rr"] >= 1.5 else 0, 1, f"about {levels['rr']} to 1 (needs 1.5)"))
    return out