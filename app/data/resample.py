"""Build the seven derived timeframes. Only completed periods are returned, so the engine
never sees a candle that is still forming."""
from datetime import datetime, timedelta, time

import pandas as pd

from ..config import SESSION_OPEN, SESSION_CLOSE, TF_MINUTES

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"}


def _session_minutes(ts: pd.Series) -> pd.Series:
    return (ts.dt.hour * 60 + ts.dt.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)


def resample_intraday(df: pd.DataFrame, minutes: int, now: datetime) -> pd.DataFrame:
    """Intraday bars -> a longer intraday bar (15m from 5m; 1h, 2h, 4h from 30m), anchored at the 09:15 open."""
    if df.empty:
        return df
    d = df.copy()
    bucket = _session_minutes(d["ts"]) // minutes
    start = d["ts"].dt.normalize() + pd.to_timedelta(SESSION_OPEN.hour * 60 + SESSION_OPEN.minute + bucket * minutes, unit="m")
    out = d.assign(ts=start).groupby("ts", as_index=False).agg(AGG)
    close_dt = out["ts"].dt.normalize() + pd.Timedelta(hours=SESSION_CLOSE.hour, minutes=SESSION_CLOSE.minute)
    end = pd.concat([out["ts"] + pd.Timedelta(minutes=minutes), close_dt], axis=1).min(axis=1)
    return out[end <= pd.Timestamp(now)].reset_index(drop=True)


def _period_start(ts: pd.Series, tf: str, fiscal: bool) -> pd.Series:
    day = ts.dt.normalize()
    if tf == "1w":
        return day - pd.to_timedelta(ts.dt.weekday, unit="D")
    if tf == "1mo":
        return day.dt.to_period("M").dt.to_timestamp()
    if tf == "3mo":
        return day.dt.to_period("Q").dt.to_timestamp()
    first = 4 if fiscal else 1                      # first month of the year in use
    idx = (ts.dt.month - first) % 12                # month index inside that year
    year = ts.dt.year - (ts.dt.month < first).astype(int)
    if tf == "6mo":
        month = first + (idx // 6) * 6
    elif tf == "1y":
        month = pd.Series(first, index=ts.index)
    else:
        raise ValueError(tf)
    year = year + (month > 12).astype(int)
    month = (month - 1) % 12 + 1
    return pd.to_datetime({"year": year, "month": month, "day": 1})


def _next_period(start: pd.Timestamp, tf: str) -> pd.Timestamp:
    if tf == "1w":
        return start + pd.Timedelta(days=7)
    months = {"1mo": 1, "3mo": 3, "6mo": 6, "1y": 12}[tf]
    return start + pd.DateOffset(months=months)


def resample_daily(df: pd.DataFrame, tf: str, now: datetime, fiscal: bool = False) -> pd.DataFrame:
    """Daily bars -> week, month, quarter, half year or year. A period counts as complete
    once its last possible session has closed (Friday 15:30 for a week)."""
    if df.empty:
        return df
    d = df.copy()
    d["ts"] = _period_start(d["ts"], tf, fiscal).values
    out = d.groupby("ts", as_index=False).agg(AGG)

    def complete(start: pd.Timestamp) -> bool:
        last_day = start + pd.Timedelta(days=4) if tf == "1w" else _next_period(start, tf) - pd.Timedelta(days=1)
        return datetime.combine(last_day.date(), SESSION_CLOSE) <= now

    return out[out["ts"].map(complete)].reset_index(drop=True)


def close_time(ts: datetime, tf: str) -> datetime:
    """When the candle that starts at ts is complete. Used to make sure a lower timeframe
    never reads a higher-timeframe candle before it has closed."""
    close = datetime.combine(ts.date(), SESSION_CLOSE)
    if tf in TF_MINUTES:
        return min(ts + timedelta(minutes=TF_MINUTES[tf]), close)
    if tf == "1d":
        return close
    if tf == "1w":
        return datetime.combine((ts + timedelta(days=4)).date(), SESSION_CLOSE)
    last_day = _next_period(pd.Timestamp(ts), tf) - pd.Timedelta(days=1)
    return datetime.combine(last_day.date(), SESSION_CLOSE)


def is_stub(ts: pd.Series, tf: str) -> pd.Series:
    """The 15:15 bar on 30m and 1h charts is only 15 minutes long."""
    if tf not in ("30m", "1h", "2h"):
        return pd.Series(False, index=ts.index)
    return (ts.dt.hour == 15) & (ts.dt.minute == 15)
