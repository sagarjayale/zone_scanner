"""GTF EYE and Premium pages: where each stock's price stands against its HTF demand and supply zones.

For one analysis mode (Daily = 1 day zones, Weekly = 1 week zones, ... Yearly = 1 year zones), every stock gets, per side:
  in_zone      the price is between the proximal and distal lines of an active zone of that side
  reacting     an active zone of that side was touched by one of the last REACT_CANDLES closed HTF candles,
               and the price is now back outside it, moving away (above a demand zone, below a supply zone),
               but not yet further from it than REACT_MAX_R times the zone height (so the move is still going on)
  approaching  the nearest zone of that side is within the approach % of the settings (outside the zone)
  None         none of these (the nearest zone and its distance are still given)
The order of preference is in_zone, reacting, approaching. Active zones are fresh or tested, never violated or used up,
and tested no more often than the max_wick_tests setting allows (as for trading): the test going on, or for reacting the
test that just ended, is not counted.

Volatility: ATR % = the average true range of the last 14 closed daily candles, as a % of the last close, for every
stock. Stocks with options also get the at-the-money IV and its IV percentile from the option snapshots the Options
page stores (app/options/chain.py); stocks without options, or with too few stored days, have only ATR %.

Everything a page needs is saved at the end of each scan (table eye_rows, one row per stock and mode), so the pages
read every stock in one query and only apply the live price. Nothing is downloaded or worked out on a page request."""
from datetime import datetime
from types import SimpleNamespace

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import MODES
from ..data.resample import close_time
from ..data.sync import load_candles, now_ist
from ..models import EyeRow, Instrument, ModeResult, Zone
from ..options.chain import iv_percentile
from .lifecycle import ACTIVE
from .score import htf_score

REACT_CANDLES = 3
REACT_MAX_R = 2.0        # reacting: still within 2 zone heights of the proximal line
ATR_DAYS = 14
# every analysis mode: its HTF zones (30 min, 1 hr, 2 hr, 4 hr, day ... year)
EYE_MODES = ("30M", "HOURLY", "2HR", "4HR", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "HALFYEARLY", "YEARLY")


def _closed(df, tf: str, now: datetime):
    """Only the candles that have closed (a derived quarter, half year or year can include the one still forming)."""
    if df.empty:
        return df
    keep = [close_time(t.to_pydatetime(), tf) <= now for t in df["ts"]]
    return df[keep]


def atr_pct(daily) -> float | None:
    """Average true range of the last ATR_DAYS closed daily candles, as a % of the last close."""
    if daily is None or len(daily) < 2:
        return None
    h, l, c = (daily[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    prev = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    tr = tr[-ATR_DAYS:]
    return round(float(np.mean(tr)) / float(c[-1]) * 100, 3) if c[-1] > 0 and len(tr) else None


def _card(z, price: float, state: str | None, th: dict | None = None) -> dict:
    lo, hi = sorted((float(z.proximal), float(z.distal)))
    if lo <= price <= hi:
        dist = 0.0
    elif z.side == "demand":
        dist = (price - float(z.proximal)) / price * 100
    else:
        dist = (float(z.proximal) - price) / price * 100
    # the score as the app gives it for trading (htf_score: the test going on is not counted under the GTF PDF rules)
    score, label = htf_score(z, th) if th else (float(z.score or 0), z.label)
    return {"zone_id": z.id, "side": z.side, "proximal": float(z.proximal), "distal": float(z.distal), "pattern": z.pattern,
            "score": float(score), "label": label, "status": z.status, "touches": int(z.touches or 0),
            "created_ts": z.created_ts.isoformat() if z.created_ts else None, "state": state, "distance_pct": round(dist, 3)}


def _tests_before(z) -> int:
    """Tests the zone had before the one going on now (or before the one that just ended, for a zone being left)."""
    return max(int(z.touches or 0) - (1 if getattr(z, "in_visit", False) else 0), 0)


def side_view(zones: list, side: str, price: float, candles: list[tuple[float, float, float]], approach_pct: float,
              max_tests: int | None = None, th: dict | None = None) -> dict | None:
    """Where the price stands against the zones of one side. candles: (high, low, close) of the last closed HTF candles,
    oldest first, optionally with the candle's open time as a 4th item. th: the HTF thresholds, for the zone score. max_tests: zones tested more often than this are left out (None: no limit).
    Returns the zone card with its state, or None when the stock has no usable zone of that side."""
    zs = [z for z in zones if z.side == side]
    if not zs or not price:
        return None
    if max_tests is not None:
        # a zone that a recent candle touched counts that touch as the reaction, not as an earlier test
        zs = [z for z in zs if _tests_before(z) <= max_tests or (int(z.touches or 0) - 1 <= max_tests and not getattr(z, "in_visit", False))]
        if not zs:
            return None
    demand = side == "demand"
    inside = [z for z in zs if min(z.proximal, z.distal) <= price <= max(z.proximal, z.distal)]
    if inside:
        return _card(max(inside, key=lambda z: (float(z.score or 0), z.created_ts or datetime.min)), price, "in_zone", th)
    # zones on the trading side of the price: below it for demand, above it for supply; nearest first
    out = sorted((z for z in zs if (z.proximal < price if demand else z.proximal > price)),
                 key=lambda z: abs(price - float(z.proximal)))
    if not out:
        return None
    recent = candles[-REACT_CANDLES:]
    if recent:
        for z in out:
            # only candles after the leg-out count: the zone's own base candles are inside it by definition
            after = [c for c in recent if len(c) < 4 or z.created_ts is None or c[3] > z.created_ts]
            touched = any((c[1] <= z.proximal) if demand else (c[0] >= z.proximal) for c in after)
            height = abs(float(z.proximal) - float(z.distal))
            if touched and height > 0 and abs(price - float(z.proximal)) <= REACT_MAX_R * height:
                card = _card(z, price, "reacting", th)
                card["reaction_r"] = round(abs(price - float(z.proximal)) / height, 2)
                return card
    if max_tests is not None:        # approaching: the test that would begin must still be allowed
        out = [z for z in out if _tests_before(z) <= max_tests]
        if not out:
            return None
    near = out[0]
    card = _card(near, price, None, th)
    if card["distance_pct"] <= float(approach_pct or 0):
        card["state"] = "approaching"
    return card


def _zone_dict(z, th: dict) -> dict:
    """What a page needs of one zone, with the score the app gives it for trading (worked out at scan time)."""
    score, label = htf_score(z, th)
    return {"id": z.id, "side": z.side, "proximal": float(z.proximal), "distal": float(z.distal), "pattern": z.pattern,
            "score": float(score), "label": label, "status": z.status, "touches": int(z.touches or 0),
            "in_visit": bool(z.in_visit), "created_ts": z.created_ts.isoformat() if z.created_ts else None}


def _zone_obj(d: dict) -> SimpleNamespace:
    return SimpleNamespace(**{**d, "created_ts": datetime.fromisoformat(d["created_ts"]) if d.get("created_ts") else None})


def rows_for(db: Session, key: str, now: datetime | None = None) -> dict[str, dict]:
    """The saved EYE data of one instrument for every EYE mode (read from the database; nothing is downloaded)."""
    now = now or now_ist()
    from .. import settings_store
    daily = _closed(load_candles(db, key, "1d", last=ATR_DAYS + 2), "1d", now)
    try:
        iv = iv_percentile(db, key, now)
    except Exception:                     # no option data for this stock
        iv = {}
    shared = {"atr_pct": atr_pct(daily), "atm_iv": iv.get("atm_iv"), "iv_pct": iv.get("percentile"),
              "last_close": float(daily["close"].iloc[-1]) if not daily.empty else None}
    saved = {r.mode: r.data or {} for r in db.scalars(select(ModeResult).where(ModeResult.instrument_key == key))}
    by_tf: dict[str, tuple] = {}
    out = {}
    for mode in EYE_MODES:
        htf = MODES[mode][0]
        if htf not in by_tf:
            th = settings_store.thresholds(db, htf)
            zones = db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.timeframe == htf, Zone.status.in_(ACTIVE))).all()
            bars = _closed(load_candles(db, key, htf, last=REACT_CANDLES + 2), htf, now)
            by_tf[htf] = ([_zone_dict(z, th) for z in zones],
                          [(float(h), float(l), float(c), t.to_pydatetime().isoformat()) for h, l, c, t in
                           zip(bars["high"], bars["low"], bars["close"], bars["ts"])][-REACT_CANDLES:])
        zones, candles = by_tf[htf]
        data = saved.get(mode) or {}
        sig = data.get("signal") or {}
        out[mode] = {**shared, "zones": zones, "candles": candles, "scan_price": data.get("cmp") or shared["last_close"],
                     "signal": sig.get("signal") or "NO_TRADE", "signal_score": sig.get("score"), "entry": sig.get("entry"),
                     "stoploss": sig.get("stoploss"), "target": sig.get("target"), "trade_side": data.get("trade_side")}
    return out


def store(db: Session, key: str, now: datetime | None = None) -> None:
    """Save the EYE rows of one instrument (called at the end of each scan)."""
    now = now or now_ist()
    for mode, data in rows_for(db, key, now).items():
        row = db.get(EyeRow, (key, mode))
        if row is None:
            row = EyeRow(instrument_key=key, mode=mode)
            db.add(row)
        row.data, row.updated_at = data, now
    db.commit()


def scan(db: Session, keys: list[str], mode: str, prices: dict, approach_pct: float, th: dict | None = None) -> tuple[list[dict], list[str]]:
    """One row per stock with its demand and supply view, ATR %, IV and the GTF signal of the saved scan, read from the
    saved EYE rows in one query; only the live price is applied here. th: the thresholds of the mode's HTF (max_wick_tests
    for which zones count). Returns the rows and the stocks that have no saved row yet (they are saved in the background)."""
    max_tests = int(th["max_wick_tests"]) if th and th.get("max_wick_tests") is not None else None
    if not keys:
        return [], []
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(Instrument.instrument_key.in_(keys))).all())
    saved = {r.instrument_key: r.data for r in db.scalars(select(EyeRow).where(EyeRow.mode == mode, EyeRow.instrument_key.in_(keys)))}
    rows, missing = [], []
    for k in keys:
        h = saved.get(k)
        if not h:
            missing.append(k)
            continue
        price = prices.get(k) or h.get("scan_price")
        if not price:
            continue
        price = float(price)
        zones = [_zone_obj(z) for z in h.get("zones") or []]
        candles = [(c[0], c[1], c[2], datetime.fromisoformat(c[3])) for c in h.get("candles") or []]
        rows.append({"instrument_key": k, "symbol": names.get(k) or k.split("|")[-1], "is_index": "_INDEX|" in k, "price": price,
                     "atr_pct": h.get("atr_pct"), "atm_iv": h.get("atm_iv"), "iv_pct": h.get("iv_pct"), "signal": h.get("signal"),
                     "signal_score": h.get("signal_score"), "entry": h.get("entry"), "stoploss": h.get("stoploss"), "target": h.get("target"),
                     "demand": side_view(zones, "demand", price, candles, approach_pct, max_tests),
                     "supply": side_view(zones, "supply", price, candles, approach_pct, max_tests)})
    return rows, missing
