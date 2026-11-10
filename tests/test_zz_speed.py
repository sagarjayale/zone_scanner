"""Page speed with a full stock list: 200 stocks with saved results, EYE rows, sync rows and stored candles. Every
call a page makes on opening must answer within 0.3 seconds, and must only read what the scans saved. (The file name
sorts last, so it runs after the other tests and leaves the stock list as it found it.)"""
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select

from app import settings_store
from app.config import TIMEFRAMES
from app.db import SessionLocal
from app.engine import eye, result
from app.main import _BUILDING, app
from app.models import Candle, EyeRow, Instrument, InstrumentStatus, ModeResult, SyncState
from tests.test_z_other_ltf_zones import build, seed

N = 200
LIMIT = 0.3
PAGES = ["/api/results?mode=MONTHLY", "/api/live/status?mode=MONTHLY", "/api/eye?mode=DAILY", "/api/eye?mode=4HR",
         "/api/paper/positions", "/api/paper/summary", "/api/scanchecks", "/api/settings", "/api/data/status", "/api/jobs",
         "/api/auth/status"]


def test_every_page_answers_quickly_with_200_stocks():
    with SessionLocal() as db:
        seed(db, "NSE_EQ|SPEED0")
        sample = build(db, "NSE_EQ|SPEED0")                       # one real result and one real EYE row, copied 200 times
        eye_rows = eye.rows_for(db, "NSE_EQ|SPEED0")
        keys = [f"NSE_EQ|SPD{n:03d}" for n in range(N)]
        now = datetime(2026, 6, 2, 15, 30)
        db.execute(insert(Instrument), [{"instrument_key": k, "symbol": k.split("|")[1], "segment": "NSE_EQ"} for k in keys])
        db.execute(insert(InstrumentStatus), [{"instrument_key": k, "state": "ready", "ready": True, "updated_at": now} for k in keys])
        db.execute(insert(ModeResult), [{"instrument_key": k, "mode": "MONTHLY", "signal": sample["signal"]["signal"],
                                         "updated_at": now, "data": {**sample, "instrument_key": k}} for k in keys])
        db.execute(insert(EyeRow), [{"instrument_key": k, "mode": m, "updated_at": now, "data": eye_rows[m]}
                                    for k in keys for m in ("DAILY", "4HR")])
        db.execute(insert(SyncState), [{"instrument_key": k, "timeframe": tf, "last_candle_ts": now, "gaps": 0, "candles": 1000}
                                       for k in keys for tf in TIMEFRAMES])
        t0 = datetime(2024, 1, 1)
        db.execute(insert(Candle), [{"instrument_key": k, "timeframe": "1d", "ts": t0 + timedelta(days=d), "open": 1.0, "high": 2.0,
                                     "low": 0.5, "close": 1.5, "volume": 1, "oi": 0} for k in keys[:100] for d in range(300)])
        earlier = list(settings_store.get(db, "universe") or [])
        settings_store.put(db, "universe", keys)
        db.commit()
    try:
        with TestClient(app) as c:
            for url in PAGES:
                assert c.get(url).status_code == 200, url          # warm up (first query plans, imports)
            slow = {}
            for url in PAGES:
                began = time.perf_counter()
                r = c.get(url)
                took = time.perf_counter() - began
                assert r.status_code == 200, url
                if took > LIMIT:
                    slow[url] = round(took, 3)
            assert not slow, f"slower than {LIMIT} s: {slow}"
            board = c.get("/api/results?mode=MONTHLY").json()
            assert board["ready"] == N and len(board["rows"]) == N and not _BUILDING
            mine = [r for r in c.get("/api/data/status").json()["rows"] if r["instrument_key"] == keys[150]]
            assert mine and all(r["candles"] == 1000 for r in mine)     # the saved count, never counted here
            assert len(c.get("/api/eye?mode=4HR").json()["rows"]) == N

            # a result that was never saved: listed as on its way, built in the background, then shown
            with SessionLocal() as db:
                db.execute(delete(ModeResult).where(ModeResult.instrument_key == keys[0]))
                db.commit()
            board = c.get("/api/results?mode=MONTHLY").json()
            assert board["ready"] == N - 1 and any(p["instrument_key"] == keys[0] and p["state"] == "scanning" for p in board["pending"])
            for _ in range(50):
                if c.get("/api/results?mode=MONTHLY").json()["ready"] == N:
                    break
                time.sleep(0.1)
            with SessionLocal() as db:
                assert db.get(ModeResult, (keys[0], "MONTHLY")) is not None
    finally:
        with SessionLocal() as db:
            settings_store.put(db, "universe", earlier)
            for model in (Instrument, InstrumentStatus, ModeResult, EyeRow, SyncState, Candle):
                db.execute(delete(model).where(model.instrument_key.in_(keys)))
            db.commit()
