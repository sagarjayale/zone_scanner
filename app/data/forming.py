"""The candle still forming on any timeframe, for drawing on the chart only.

It is built from what is already stored plus the live price: the closed 5 minute candles of the period so far (and,
for a week or longer, the closed daily candles of the period before today), then the live price stretches its high or
low and sets its close. Nothing is downloaded and nothing is stored. The scan never sees it: zones are found and
tested on closed candles only."""
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from ..config import SESSION_CLOSE, SESSION_OPEN, TF_MINUTES
from .resample import _period_start
from .sync import load_candles


def period_start(now: datetime, tf: str, fiscal: bool = False) -> datetime | None:
    """Start of the period of timeframe tf that contains `now` (session time, IST). None outside the session."""
    open_dt, close_dt = datetime.combine(now.date(), SESSION_OPEN), datetime.combine(now.date(), SESSION_CLOSE)
    if not (open_dt <= now < close_dt):
        return None
    if tf in TF_MINUTES:
        m = TF_MINUTES[tf]
        return open_dt + timedelta(minutes=((now - open_dt).seconds // 60) // m * m)
    if tf == "1d":
        return datetime.combine(now.date(), datetime.min.time())
    return _period_start(pd.Series([pd.Timestamp(now)]), tf, fiscal).iloc[0].to_pydatetime()


def candle(db: Session, key: str, tf: str, price: float | None, now: datetime, fiscal: bool = False) -> dict | None:
    """The forming candle of tf at `now`, or None (market closed, or no live price and nothing stored for it yet)."""
    start = period_start(now, tf, fiscal)
    if start is None:
        return None
    today = datetime.combine(now.date(), SESSION_OPEN)
    parts = []
    if tf not in TF_MINUTES and tf != "1d":           # week and longer: the closed days of the period before today
        days = load_candles(db, key, "1d", since=start, until=today - timedelta(seconds=1))
        if not days.empty:
            parts.append(days)
    five = load_candles(db, key, "5m", since=max(start, today))
    if not five.empty:
        parts.append(five[five["ts"] < pd.Timestamp(now)])
    rows = pd.concat(parts) if parts else pd.DataFrame()
    if rows.empty and price is None:
        return None
    o = float(rows["open"].iloc[0]) if not rows.empty else float(price)
    h = float(rows["high"].max()) if not rows.empty else float(price)
    l = float(rows["low"].min()) if not rows.empty else float(price)
    c = float(rows["close"].iloc[-1]) if not rows.empty else float(price)
    if price is not None:
        h, l, c = max(h, float(price)), min(l, float(price)), float(price)
    return {"ts": start.isoformat(), "open": o, "high": h, "low": l, "close": c, "forming": True}
