"""History windows of scanner.py v6.4. It reads each timeframe only from a start date that depends on the mode and on
the timeframe, so zones older than that start are never seen. The app stores more history than that; with the setting
zone_history = "scanner" (the default) zones whose base starts before the window are left out, so the app sees the same
zones as scanner.py. zone_history = "all" uses every stored zone.

scanner.py (_date_windows), for the date of the scan:
  5 and 15 min      one month back
  30 min, 1 to 4 hr from the first day of the current quarter
  day               the later of ten years back and the mode's start
  week and above    the later of 2000-01-01 and the mode's start
  mode start        Hourly one year back, Daily five years back, Weekly 2010-01-01, every other mode 2000-01-01
"""
import calendar
from datetime import date, datetime, time

UNIT = {"5m": ("minutes", 5), "15m": ("minutes", 15), "30m": ("minutes", 30), "1h": ("hours", 1), "2h": ("hours", 2),
        "4h": ("hours", 4), "1d": ("days", 1), "1w": ("weeks", 1), "1mo": ("months", 1), "3mo": ("months", 3),
        "6mo": ("months", 6), "1y": ("months", 12)}
EARLIEST = date(2000, 1, 1)


def _years_back(d: date, n: int) -> date:
    return d.replace(year=d.year - n, day=min(d.day, calendar.monthrange(d.year - n, d.month)[1]))


def _month_back(d: date) -> date:
    year, month = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _quarter_start(d: date) -> date:
    return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)


def _mode_start(mode: str, end: date) -> date:
    return {"HOURLY": _years_back(end, 1), "DAILY": _years_back(end, 5), "WEEKLY": date(2010, 1, 1)}.get(mode, EARLIEST)


def start(mode: str, tf: str, end, th: dict) -> datetime | None:
    """First moment of the window scanner.py reads for timeframe tf in this mode, or None to use all history."""
    if (th.get("zone_history") or "scanner") == "all" or end is None:
        return None
    end = end.date() if isinstance(end, datetime) else end
    unit, size = UNIT[tf]
    if unit == "minutes":
        d = _month_back(end) if size <= 15 else _quarter_start(end)
    elif unit == "hours":
        d = _quarter_start(end)
    elif unit == "days":
        d = max(_years_back(end, 10), _mode_start(mode, end))
    else:
        d = max(EARLIEST, _mode_start(mode, end))
    return datetime.combine(d, time())


def keep(zones: list, since: datetime | None) -> list:
    """The zones whose base starts inside the window (so their leg-in is inside it too)."""
    if since is None:
        return list(zones)
    return [z for z in zones if z.base_start_ts is not None and z.base_start_ts > since]
