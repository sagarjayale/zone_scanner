"""Upstox REST client: login, candles, instruments. All calls are throttled to stay inside
the published limits (25 per second, 250 per minute, 1000 per 30 minutes)."""
import asyncio
import gzip
import json
import time
from collections import deque
from datetime import date, datetime
from urllib.parse import quote, urlencode

import httpx

from .. import config

BASE = "https://api.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.json.gz"


class UpstoxError(Exception):
    pass


class TokenMissing(UpstoxError):
    pass


class Throttle:
    """Sliding-window limiter kept a little under each published limit."""
    LIMITS = ((1, 20), (60, 230), (1800, 950))

    def __init__(self):
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > 1800:
                    self.calls.popleft()
                delay = 0.0
                for window, limit in self.LIMITS:
                    recent = [t for t in self.calls if now - t < window]
                    if len(recent) >= limit:
                        delay = max(delay, window - (now - recent[0]) + 0.01)
                if delay <= 0:
                    self.calls.append(now)
                    return
                await asyncio.sleep(delay)

    def used(self) -> dict:
        now = time.monotonic()
        return {f"{w}s": sum(1 for t in self.calls if now - t < w) for w, _ in self.LIMITS}


def parse_candle(row: list) -> dict:
    """Upstox candle: [timestamp, open, high, low, close, volume, open interest]."""
    ts = datetime.fromisoformat(row[0]).replace(tzinfo=None)  # keep exchange (IST) wall time
    return {"ts": ts, "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
            "close": float(row[4]), "volume": int(row[5] or 0), "oi": int(row[6] or 0) if len(row) > 6 else 0}


class UpstoxClient:
    def __init__(self, token_getter):
        self._token_getter = token_getter  # callable returning the current access token or ""
        self.throttle = Throttle()
        self.http = httpx.AsyncClient(timeout=30)

    # ---- login ----
    @staticmethod
    def login_url(state: str = "scanner") -> str:
        q = urlencode({"response_type": "code", "client_id": config.API_KEY,
                       "redirect_uri": config.REDIRECT_URI, "state": state})
        return f"{BASE}/v2/login/authorization/dialog?{q}"

    async def exchange_code(self, code: str) -> str:
        r = await self.http.post(f"{BASE}/v2/login/authorization/token",
                                 headers={"Accept": "application/json"},
                                 data={"code": code, "client_id": config.API_KEY, "client_secret": config.API_SECRET,
                                       "redirect_uri": config.REDIRECT_URI, "grant_type": "authorization_code"})
        if r.status_code != 200:
            raise UpstoxError(f"Login failed: {r.status_code} {r.text[:200]}")
        return r.json()["access_token"]

    # ---- low level ----
    async def _get(self, path: str, params: dict | None = None) -> dict:
        token = self._token_getter()
        if not token:
            raise TokenMissing("No Upstox access token. Log in first.")
        await self.throttle.wait()
        r = await self.http.get(f"{BASE}{path}", params=params,
                                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            raise TokenMissing("Upstox token expired or invalid. Log in again.")
        if r.status_code != 200:
            raise UpstoxError(f"{path}: {r.status_code} {r.text[:200]}")
        return r.json()

    # ---- candles ----
    async def historical(self, key: str, unit: str, interval: int, to_date: date, from_date: date) -> list[dict]:
        path = f"/v3/historical-candle/{quote(key, safe='')}/{unit}/{interval}/{to_date}/{from_date}"
        data = await self._get(path)
        return [parse_candle(c) for c in data.get("data", {}).get("candles", [])]

    async def intraday(self, key: str, unit: str, interval: int) -> list[dict]:
        path = f"/v3/historical-candle/intraday/{quote(key, safe='')}/{unit}/{interval}"
        data = await self._get(path)
        return [parse_candle(c) for c in data.get("data", {}).get("candles", [])]

    async def ltp(self, keys: list[str]) -> dict[str, float]:
        """Last traded price for many instruments, 400 per request. Returns {instrument_key: price}.
        Upstox keys the response by symbol, so the instrument_token field is used to map back."""
        out: dict[str, float] = {}
        for i in range(0, len(keys), 400):
            data = await self._get("/v3/market-quote/ltp", {"instrument_key": ",".join(keys[i:i + 400])})
            for item in (data.get("data") or {}).values():
                if item.get("instrument_token") and item.get("last_price"):
                    out[item["instrument_token"]] = float(item["last_price"])
        return out

    async def option_chain(self, key: str, expiry: str) -> list[dict]:
        """Put/call option chain for one underlying and one expiry (YYYY-MM-DD)."""
        data = await self._get("/v2/option/chain", {"instrument_key": key, "expiry_date": expiry})
        return data.get("data") or []

    async def profile(self) -> dict:
        return await self._get("/v2/user/profile")

    # ---- instruments (public file, no token) ----
    async def instruments(self, exchange: str = "NSE") -> list[dict]:
        r = await self.http.get(INSTRUMENTS_URL.format(exchange=exchange), timeout=120)
        if r.status_code != 200:
            raise UpstoxError(f"Instruments download failed: {r.status_code}")
        return json.loads(gzip.decompress(r.content))

    async def close(self):
        await self.http.aclose()
