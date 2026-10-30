"""Option chain view, suggested strike, and the saved snapshots that IV percentile is built from."""
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import settings_store
from ..engine.confirm import buildup
from ..models import Instrument, OptionSnapshot

NEAR = 12            # strikes shown on each side of the money
IV_DAYS = 252
MIN_IV_DAYS = 20     # below this the percentile is not shown


def expiries(db: Session, key: str, today) -> list[str]:
    rows = db.execute(select(func.date(Instrument.expiry)).where(
        Instrument.underlying_key == key, Instrument.segment == "NSE_FO", Instrument.instrument_type.in_(("CE", "PE")),
        Instrument.expiry >= datetime.combine(today, datetime.min.time())).distinct()).all()
    return sorted(str(r[0]) for r in rows if r[0])


def _leg(raw: dict, max_spread: float, min_oi: float) -> dict | None:
    if not raw:
        return None
    m, g = raw.get("market_data") or {}, raw.get("option_greeks") or {}
    bid, ask, ltp = m.get("bid_price") or 0, m.get("ask_price") or 0, m.get("ltp") or 0
    mid = (bid + ask) / 2
    spread = round(100 * (ask - bid) / mid, 2) if bid > 0 and ask > 0 and mid > 0 else None
    oi = m.get("oi") or 0
    change = oi - (m.get("prev_oi") or 0)
    return {"instrument_key": raw.get("instrument_key"), "ltp": ltp, "oi": oi, "oi_change": change, "close": m.get("close_price"),
            "buildup": buildup(ltp, m.get("close_price"), change), "gamma": g.get("gamma"), "theta": g.get("theta"),
            "volume": m.get("volume") or 0, "bid": bid, "ask": ask, "spread_pct": spread, "iv": g.get("iv"), "delta": g.get("delta"),
            "liquid": bool(spread is not None and spread <= max_spread and oi >= min_oi)}


def build_view(db: Session, rows: list[dict], zone=None) -> dict:
    """Turn the Upstox chain into the table the page shows, with totals and a suggested strike."""
    max_spread = float(settings_store.get(db, "option_max_spread_pct"))
    min_oi = float(settings_store.get(db, "option_min_oi"))
    if not rows:
        return {"spot": None, "strikes": [], "pcr": None, "atm": None, "suggestion": None, "call_wall": None, "put_wall": None, "suggested_leg": None}
    rows = sorted(rows, key=lambda r: r["strike_price"])
    spot = rows[0].get("underlying_spot_price")
    strikes = [{"strike": r["strike_price"], "call": _leg(r.get("call_options"), max_spread, min_oi),
                "put": _leg(r.get("put_options"), max_spread, min_oi)} for r in rows]
    call_oi = sum((s["call"] or {}).get("oi", 0) for s in strikes)
    put_oi = sum((s["put"] or {}).get("oi", 0) for s in strikes)
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i]["strike"] - spot)) if spot else len(strikes) // 2
    near = strikes[max(0, atm_i - NEAR):atm_i + NEAR + 1]
    wall = lambda leg: max((x for x in strikes if x[leg] and x[leg]["oi"] > 0), key=lambda x: x[leg]["oi"], default=None)
    cw, pw = wall("call"), wall("put")
    sug = suggest(strikes, zone) if zone else None
    picked = next((x for x in strikes if sug and sug["buy"] and x["strike"] == sug["buy"]["strike"]), None)
    return {"spot": spot, "atm": strikes[atm_i]["strike"], "pcr": round(put_oi / call_oi, 2) if call_oi else None,
            "call_oi": call_oi, "put_oi": put_oi, "strikes": near, "suggestion": sug,
            "call_wall": cw["strike"] if cw else None, "put_wall": pw["strike"] if pw else None,
            "suggested_leg": picked[sug["buy"]["type"]] if picked else None}


def suggest(strikes: list[dict], zone) -> dict:
    """Buy: the liquid option on the zone's side with delta nearest 0.55 (between 0.5 and 0.6 if possible).
    Sell: the first strike beyond the zone's distal line, as the short leg of a spread."""
    leg = "call" if zone.side == "demand" else "put"
    cands = [(abs(abs(s[leg]["delta"]) - 0.55), s) for s in strikes if s[leg] and s[leg].get("delta") is not None]
    liquid = [c for c in cands if c[1][leg]["liquid"]]
    best = min(liquid or cands, key=lambda c: c[0], default=None)
    if zone.side == "demand":
        beyond = [s for s in strikes if s["strike"] < zone.distal and s["put"]]
        short = beyond[-1] if beyond else None
    else:
        beyond = [s for s in strikes if s["strike"] > zone.distal and s["call"]]
        short = beyond[0] if beyond else None
    return {"buy": {"type": leg, "strike": best[1]["strike"], "delta": best[1][leg]["delta"], "ltp": best[1][leg]["ltp"],
                    "liquid": best[1][leg]["liquid"], "instrument_key": best[1][leg]["instrument_key"]} if best else None,
            "sell": {"type": "put" if zone.side == "demand" else "call", "strike": short["strike"]} if short else None}


def save_snapshot(db: Session, key: str, expiry: str, rows: list[dict], now: datetime) -> int:
    """Store open interest, IV and price for strikes near the money, plus one ATM row used for IV percentile."""
    if not rows:
        return 0
    rows = sorted(rows, key=lambda r: r["strike_price"])
    spot = rows[0].get("underlying_spot_price") or 0
    atm_i = min(range(len(rows)), key=lambda i: abs(rows[i]["strike_price"] - spot))
    n = 0
    for r in rows[max(0, atm_i - 10):atm_i + 11]:
        for typ, side in (("CE", "call_options"), ("PE", "put_options")):
            m, g = (r.get(side) or {}).get("market_data") or {}, (r.get(side) or {}).get("option_greeks") or {}
            db.add(OptionSnapshot(underlying_key=key, expiry=expiry, strike=r["strike_price"], option_type=typ, ts=now,
                                  oi=m.get("oi"), iv=g.get("iv"), ltp=m.get("ltp")))
            n += 1
    ivs = [((rows[atm_i].get(s) or {}).get("option_greeks") or {}).get("iv") for s in ("call_options", "put_options")]
    ivs = [v for v in ivs if v]
    if ivs:
        db.add(OptionSnapshot(underlying_key=key, expiry=expiry, strike=rows[atm_i]["strike_price"], option_type="ATM", ts=now,
                              iv=sum(ivs) / len(ivs), ltp=spot))
    db.commit()
    return n


def iv_percentile(db: Session, key: str, now: datetime) -> dict:
    """Share of the last 252 stored days on which at-the-money IV was below the latest value."""
    since = now - timedelta(days=int(IV_DAYS * 1.5))
    rows = db.execute(select(OptionSnapshot.ts, OptionSnapshot.iv).where(
        OptionSnapshot.underlying_key == key, OptionSnapshot.option_type == "ATM", OptionSnapshot.ts >= since)
        .order_by(OptionSnapshot.ts)).all()
    daily = {}
    for ts, iv in rows:
        if iv:
            daily[ts.date()] = iv      # the last snapshot of each day
    days = list(daily.values())[-IV_DAYS:]
    if len(days) < MIN_IV_DAYS:
        return {"days": len(days), "needed": MIN_IV_DAYS, "percentile": None, "atm_iv": days[-1] if days else None}
    return {"days": len(days), "needed": MIN_IV_DAYS, "atm_iv": days[-1],
            "percentile": round(100 * sum(1 for v in days[:-1] if v < days[-1]) / (len(days) - 1), 1)}


def prune(db: Session, now: datetime) -> None:
    db.execute(delete(OptionSnapshot).where(OptionSnapshot.ts < now - timedelta(days=400)))
    db.commit()