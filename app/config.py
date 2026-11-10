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
    "zone_history": "scanner",   # scanner: only zones inside scanner.py's history window for each mode and timeframe; all: every stored zone
    "htf_zone_pick": "fresh",    # fresh: the nearest HTF demand and supply must never have been tested (GTF PDF page 31); any: any live zone (scanner.py)
    "ltf_min_bases": 0,          # an LTF execution zone with fewer base candles than this is not traded (0 = off)
    "ltf_max_bases": 0,          # an LTF execution zone with more base candles than this is not traded (0 = off)
    "min_stop_pct": 0.0,         # an LTF execution zone whose stop is closer than this % of price is not traded (0 = off): charges eat thin zones
    "skip_reaction_zones": False,  # GTF PDF p.35-36: a zone formed in the reaction of an earlier zone is not traded, unless its leg-out is strong
    "check_next_htf": False,     # GTF PDF p.30: no trade when a fresh opposite zone one timeframe above the HTF lies between the entry and the target
    "itf_rule": "pct",           # ITF trend threshold. pct: a share of price (scanner.py); atr: a share of the average move per candle
    "itf_slope_pct": 0.05,       # pct rule: the 50 SMA must move more than this % of price over 7 candles
    "itf_atr_mult": 0.1,         # atr rule: the 50 SMA must move more than this many average moves (last 14 candles) over 7 candles
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
    "ltf_alerts_all_zones": False,   # alerts and automatic paper orders on every LTF zone, not only each mode's default (best) zone
    # NSE equity charges, used for "costs in R". Starting values only: check them against your broker's contract note.
    "nse_product": "delivery",       # delivery | intraday
    "brokerage_per_order": 20.0,     # rupees per executed order (0 for a zero-brokerage delivery account) ...
    "brokerage_pct": 0.03,           # ... or this % of the order value, whichever is lower
    "stt_delivery_pct": 0.1,         # STT on delivery: buy and sell
    "stt_intraday_pct": 0.025,       # STT on intraday: sell side only
    "exchange_txn_pct": 0.00297,     # NSE transaction charge, both sides
    "sebi_fee_per_crore": 10.0,      # SEBI turnover fee, both sides
    "gst_pct": 18.0,                 # GST on brokerage, exchange and SEBI charges
    "stamp_delivery_pct": 0.015,     # stamp duty on the buy side, delivery
    "stamp_intraday_pct": 0.003,     # stamp duty on the buy side, intraday
    # paper trading
    "paper_capital": 1000000.0, "paper_risk_pct": 1.0, "paper_slippage_pct": 0.05,
    "paper_auto": False, "paper_auto_min_score": 8,
    "paper_auto_closed": False,      # also place automatic orders after a scan while the market is closed (from the saved scan)
    # option paper trading (its own account): buy a call on a BUY signal, a put on a SELL signal, managed on the stock's price
    "opt_paper_capital": 500000.0, "opt_paper_risk_pct": 1.0,
    "opt_strike_rule": "delta",      # delta (nearest 0.55) | atm | otm1 (one strike out of the money) | target (strike nearest the target)
    "opt_min_days": 7,               # nearest expiry with at least this many days left, else the next one
    "opt_max_premium_pct": 20.0,     # one trade's premium at most this % of the option account balance
    "opt_auto": False, "opt_auto_min_score": 8,
    "opt_auto_closed": False,        # automatic option orders also after a scan while the market is closed (last chain prices)
    "opt_expiry_close": "15:15",     # positions are closed at this time on their expiry day
    "opt_premium_stop_pct": 0.0,     # extra stop on the premium: close when it falls this % below the fill (0 = off)
    "opt_stt_pct": 0.1,              # STT on options: sell side, on the premium
    "opt_exchange_pct": 0.03503,     # exchange transaction charge on options premium, both sides
    # trailing stop loss (sim/trailing.py), for paper trading and the scan-date check. Lock on live prices by default; off: every trade keeps its first stop.
    "trail_method": "lock",      # off | step | swing | atr | lock
    "trail_start_r": 1.0,        # trailing starts once price has gone this many R in the trade's favour
    "trail_keep_target": True,   # keep the fixed target while trailing (false: remove it once trailing starts and let the trade run)
    "trail_atr_mult": 2.0,       # atr: the stop stays this many ATRs behind the best price
    "trail_swing": 2,            # swing: candles on each side of a swing low (or high)
    "trail_tf": "live",          # what moves the stop: live (each live price, at once), ltf (the trade's own candles) or itf (the mode's middle timeframe)
    "trail_lock_pct": 10.0,      # lock: one step is this % of the distance from the entry to the first target
    "trail_lock_gap_pct": 20.0,  # lock: price must pass a step by this % of one step before the stop moves to it
    "trail_lock_behind": False,  # lock: keep the stop one step behind (step 1 locks break-even) instead of on the step
    "trail_lock_target": "move", # lock: at the target, "move" it forward one step (the trade runs) or "close" there
    # options
    "option_max_spread_pct": 2.0, "option_min_oi": 1000,
    # alerts
    "telegram_alerts": True,
}