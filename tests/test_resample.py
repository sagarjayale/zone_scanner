from datetime import datetime, timedelta

import pandas as pd

from app.data.resample import is_stub, resample_daily, resample_intraday


def bars_30m(day: datetime) -> pd.DataFrame:
    ts = [day.replace(hour=9, minute=15) + timedelta(minutes=30 * i) for i in range(13)]
    return pd.DataFrame({"ts": pd.to_datetime(ts), "open": range(100, 113), "high": range(105, 118),
                         "low": range(95, 108), "close": range(101, 114), "volume": 10, "oi": 0})


def test_one_hour_and_four_hour_bars():
    day = datetime(2026, 10, 1)
    df = bars_30m(day)
    after_close = day.replace(hour=16)
    h1 = resample_intraday(df, 60, after_close)
    assert len(h1) == 7 and h1["ts"].iloc[-1] == pd.Timestamp("2026-10-01 15:15")
    assert (h1["open"].iloc[0], h1["close"].iloc[0], h1["high"].iloc[0], h1["volume"].iloc[0]) == (100, 102, 106, 20)
    h4 = resample_intraday(df, 240, after_close)
    assert list(h4["ts"]) == [pd.Timestamp("2026-10-01 09:15"), pd.Timestamp("2026-10-01 13:15")]


def test_forming_bar_is_excluded():
    day = datetime(2026, 10, 1)
    df = bars_30m(day).iloc[:3]                       # 09:15, 09:45, 10:15 bars exist
    h1 = resample_intraday(df, 60, day.replace(hour=10, minute=45))
    assert list(h1["ts"]) == [pd.Timestamp("2026-10-01 09:15")]   # 10:15 to 11:15 still forming


def test_stub_flag():
    df = bars_30m(datetime(2026, 10, 1))
    assert is_stub(df["ts"], "30m").sum() == 1 and is_stub(df["ts"], "5m").sum() == 0


def daily(start: str, end: str) -> pd.DataFrame:
    days = pd.bdate_range(start, end)
    return pd.DataFrame({"ts": days, "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 1, "oi": 0})


def test_week_month_year_only_when_complete():
    df = daily("2026-01-01", "2026-10-01")            # last day is a Thursday
    now = datetime(2026, 10, 1, 16, 0)
    w = resample_daily(df, "1w", now)
    assert w["ts"].iloc[-1] == pd.Timestamp("2026-09-21")          # week of 28 Sep not complete until Friday
    m = resample_daily(df, "1mo", now)
    assert m["ts"].iloc[-1] == pd.Timestamp("2026-09-01")
    q = resample_daily(df, "3mo", now)
    assert list(q["ts"]) == [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-04-01"), pd.Timestamp("2026-07-01")]
    assert resample_daily(df, "1y", now).empty                      # 2026 is still forming


def test_half_year_calendar_and_financial():
    df = daily("2025-01-01", "2026-10-01")
    now = datetime(2026, 10, 1, 16, 0)
    cal = resample_daily(df, "6mo", now)
    assert list(cal["ts"]) == [pd.Timestamp(x) for x in ("2025-01-01", "2025-07-01", "2026-01-01")]
    fin = resample_daily(df, "6mo", now, fiscal=True)
    assert list(fin["ts"]) == [pd.Timestamp(x) for x in ("2024-10-01", "2025-04-01", "2025-10-01", "2026-04-01")]
    fy = resample_daily(df, "1y", now, fiscal=True)
    assert list(fy["ts"]) == [pd.Timestamp("2024-04-01"), pd.Timestamp("2025-04-01")]


def test_two_hour_and_fifteen_minute_bars():
    day = datetime(2026, 10, 1)
    h2 = resample_intraday(bars_30m(day), 120, day.replace(hour=16))
    assert [t.strftime("%H:%M") for t in h2["ts"]] == ["09:15", "11:15", "13:15", "15:15"] and h2["volume"].tolist() == [40, 40, 40, 10]
    five = pd.DataFrame({"ts": pd.to_datetime([day.replace(hour=9, minute=15) + timedelta(minutes=5 * i) for i in range(75)]),
                         "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1, "oi": 0})
    m15 = resample_intraday(five, 15, day.replace(hour=16))
    assert len(m15) == 25 and m15["volume"].iloc[0] == 3 and m15["ts"].iloc[-1] == pd.Timestamp("2026-10-01 15:15")
    assert len(resample_intraday(five, 15, day.replace(hour=9, minute=40))) == 1      # 09:30 to 09:45 still forming
