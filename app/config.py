"""Settings, timeframes and default thresholds. Thresholds can be overridden in the settings table."""
import os
from datetime import date, time
from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv()

API_KEY = os.getenv("UPSTOX_API_KEY", "")
API_SECRET = os.getenv("UPSTOX_API_SECRET", "")
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")
ENV_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def database_url():
    """DATABASE_URL wins if set. Otherwise the DB_* values select MySQL. With neither, a local SQLite file.
    The MySQL URL is built field by field, so a password with @ : / or # needs no escaping in .env."""
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    if os.getenv("DB_NAME"):
        return URL.create("mysql+pymysql", username=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""),
                          host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", "3306")),
                          database=os.getenv("DB_NAME"), query={"charset": "utf8mb4"})
    return "sqlite:///./scanner.db"


DATABASE_URL = database_url()

SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)

# Ordered highest to lowest. 15 min and 2 hr are built from 5 min and 30 min.
TIMEFRAMES = ["1y", "6mo", "3mo", "1mo", "1w", "1d", "4h", "2h", "1h", "30m", "15m", "5m"]
TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}

# Fetched from Upstox: unit, interval, days per request, history start (None = rolling window in days).
FETCHED = {
    "5m": {"unit": "minutes", "interval": 5, "chunk_days": 28, "start": None, "keep_days": 180},
    "30m": {"unit": "minutes", "interval": 30, "chunk_days": 85, "start": date(2022, 1, 1)},
    "1d": {"unit": "days", "interval": 1, "chunk_days": 3600, "start": date(2000, 1, 1)},
}
# Built by resampling: derived timeframe -> source timeframe.
DERIVED = {"15m": "5m", "1h": "30m", "2h": "30m", "4h": "30m", "1w": "1d", "1mo": "1d", "3mo": "1d", "6mo": "1d", "1y": "1d"}

# GTF analysis modes: (HTF for location and curve, ITF for trend, LTF for the execution zone).
MODES = {
    "30M": ("30m", "15m", "5m"), "HOURLY": ("1h", "30m", "5m"), "2HR": ("2h", "30m", "5m"), "4HR": ("4h", "1h", "15m"),
    "DAILY": ("1d", "1h", "30m"), "WEEKLY": ("1w", "1d", "1h"), "MONTHLY": ("1mo", "1w", "1d"),
    "QUARTERLY": ("3mo", "1mo", "1d"), "HALFYEARLY": ("6mo", "3mo", "1w"), "YEARLY": ("1y", "6mo", "1mo"),
}

DEFAULTS = {
    # GTF zone rules
    "base_body_pct": 50.0,       # base candle: body strictly below this share of the range
    "leg_body_pct": 50.0,        # explosive candle: body strictly above this share of the range
    "max_base": 6,
    "marking_rule": "scanner",   # scanner: zones found, tested and scored as scanner.py v6.4 does; pdf: as the GTF course PDF marks them
    "zone_history": "scanner",
    "htf_zone_pick": "fresh",    # fresh: the nearest HTF demand and supply must never have been tested (GTF PDF page 31); any: any live zone (scanner.py)   # scanner: only zones inside scanner.py's history window for each mode and timeframe; all: every stored zone
    "gap_exciting_pct": 0.25,    # a candle that opens at least this far (% of price) beyond the previous high or low counts as exciting
    "max_wick_tests": 1,         # earlier tests an execution zone may have; one more test uses the zone up
    "min_score": 5.0,            # 7 = entry type 1 only, 5 = entry types 2 and 3 as well
    "min_htf_score": 0.0,
    "ema_period": 20,            # an EMA of either period running through the zone adds 1 point
    "ema_period_2": 50,
    "entry_23": "TYPE_3",        # which entry a score of 5 to 6.5 takes: TYPE_2 (next candle opens inside) or TYPE_3 (next candle leaves)
    "approach_pct": 1.0,         # "approaching" when price is within this percentage of the proximal line
    "ltf_max_gap_pct": 50.0,     # an execution zone further than this from price is ignored
    "htf_buffer_pct": 10.0,      # execution zones may sit this far beyond the HTF distal line, as a share of the HTF zone width
    "confirm_volume_mult": 1.5,  # confirmation: leg-out volume at least this many times the average before it
    "contact_touch_pct": 0.1,    # HTF contact: zones this close (as a percentage of price) are touching
    "contact_near_pct": 1.0,     # HTF contact: zones this close are near
    "direction_rule": "location",  # location: the curve decides, the ITF only in equilibrium; trend: the ITF decides
    "target_rr": 2.0,            # target as a multiple of entry to stop loss, for every entry type
    "modes": list(MODES),        # analysis modes the scanner runs
    "fiscal_year": False,
    "universe": ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"],
    # paper trading
    "paper_capital": 1000000.0, "paper_risk_pct": 1.0, "paper_slippage_pct": 0.05,
    "paper_auto": False, "paper_auto_min_score": 8,
    # options
    "option_max_spread_pct": 2.0, "option_min_oi": 1000,
    # alerts
    "telegram_alerts": True,
}