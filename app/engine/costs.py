"""NSE equity charges for one round trip (buy and sell), and what they cost in R. All rates come from settings.

brokerage   per order: the lower of a flat amount and a percentage of the order value
STT         delivery: both sides; intraday: sell side only
exchange    NSE transaction charge, both sides
SEBI        turnover fee per crore, both sides
GST         on brokerage + exchange + SEBI charges
stamp duty  buy side only"""
import math


def round_trip(buy_value: float, sell_value: float, th: dict) -> float:
    intraday = (th.get("nse_product") or "delivery") == "intraday"
    pct = lambda key: float(th.get(key) or 0) / 100.0

    def brokerage(value: float) -> float:
        flat, share = float(th.get("brokerage_per_order") or 0), pct("brokerage_pct") * value
        return min(flat, share) if flat > 0 and share > 0 else max(flat, share)
    turnover = buy_value + sell_value
    brok = brokerage(buy_value) + brokerage(sell_value)
    stt = pct("stt_intraday_pct") * sell_value if intraday else pct("stt_delivery_pct") * turnover
    exchange = pct("exchange_txn_pct") * turnover
    sebi = float(th.get("sebi_fee_per_crore") or 0) * turnover / 1e7
    gst = pct("gst_pct") * (brok + exchange + sebi)
    stamp = pct("stamp_intraday_pct" if intraday else "stamp_delivery_pct") * buy_value
    return brok + stt + exchange + sebi + gst + stamp


def paper_quantity(entry: float, stop: float, capital: float, risk_pct: float) -> int:
    per_share = abs(entry - stop)
    return math.floor(capital * risk_pct / 100.0 / per_share) if per_share > 0 else 0


def costs_in_r(entry: float, stop: float, side: str, th: dict, capital: float, risk_pct: float) -> float | None:
    """Charges for a trade sized like a paper trade (risking risk_pct of capital), as a share of its 1 R.
    The round trip is valued at the entry price on both sides."""
    qty = paper_quantity(entry, stop, capital, risk_pct)
    if qty <= 0:
        return None
    value = entry * qty
    return round(round_trip(value, value, th) / (abs(entry - stop) * qty), 3)
