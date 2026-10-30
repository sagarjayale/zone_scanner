"""Index constituents for the stock list. The members are read from the official NSE Indices
file each time, so the list is always the current one; nothing is hard-coded here."""
import csv
import io
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Instrument

SOURCE = "https://www.niftyindices.com/IndexConstituent/{file}"
INDICES = {   # name shown on the page -> constituents file on niftyindices.com
    "nifty50": ("Nifty 50", "ind_nifty50list.csv"),
    "niftynext50": ("Nifty Next 50", "ind_niftynext50list.csv"),
    "niftybank": ("Nifty Bank", "ind_niftybanklist.csv"),
    "niftymidcap100": ("Nifty Midcap 100", "ind_niftymidcap100list.csv"),
}
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Accept": "text/csv,*/*"}


class IndexListError(Exception):
    pass


async def fetch_csv(name: str) -> tuple[str, str]:
    """Download the constituents file. Returns (text, url)."""
    if name not in INDICES:
        raise IndexListError(f"Unknown index. Choose one of: {', '.join(INDICES)}.")
    url = SOURCE.format(file=INDICES[name][1])
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS) as http:
            r = await http.get(url)
    except Exception as exc:
        raise IndexListError(f"Could not reach niftyindices.com ({type(exc).__name__}). Paste the symbols in the box below instead.")
    if r.status_code != 200 or "ISIN" not in r.text[:400]:
        raise IndexListError(f"niftyindices.com did not return the list (status {r.status_code}). Paste the symbols in the box below instead.")
    return r.text, url


def parse_csv(text: str) -> list[dict]:
    """Rows of the NSE Indices file: Company Name, Industry, Symbol, Series, ISIN Code."""
    out = []
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if row.get("symbol") and row.get("isin code"):
            out.append({"symbol": row["symbol"], "isin": row["isin code"], "name": row.get("company name", ""), "industry": row.get("industry", "")})
    return out


def match(db: Session, wanted: list[dict], in_list: set[str]) -> list[dict]:
    """Match each row to an Upstox instrument: by ISIN first (the NSE equity key is NSE_EQ|<ISIN>), then by symbol."""
    stocks = db.scalars(select(Instrument).where(Instrument.segment.in_(("NSE_EQ", "NSE_INDEX")))).all()
    by_key = {i.instrument_key: i for i in stocks}
    by_symbol = {}
    for i in stocks:
        if i.segment == "NSE_EQ" or i.symbol.upper() not in by_symbol:
            by_symbol[i.symbol.upper()] = i
    rows = []
    for w in wanted:
        inst = by_key.get(f"NSE_EQ|{w['isin']}") if w.get("isin") else None
        inst = inst or by_symbol.get((w.get("symbol") or "").upper())
        rows.append({"symbol": inst.symbol if inst else w.get("symbol") or w.get("isin"), "name": (inst.name if inst else None) or w.get("name") or "",
                     "industry": w.get("industry", ""), "instrument_key": inst.instrument_key if inst else None,
                     "found": inst is not None, "in_list": bool(inst and inst.instrument_key in in_list)})
    return rows


def parse_pasted(text: str) -> list[dict]:
    """Symbols or ISINs typed or pasted by hand, separated by commas, spaces or new lines."""
    out, seen = [], set()
    for token in re.split(r"[\s,;]+", text.strip()):
        token = token.strip().upper()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append({"isin": token, "symbol": ""} if re.fullmatch(r"IN[A-Z0-9]{10}", token) else {"symbol": token, "isin": ""})
    return out
