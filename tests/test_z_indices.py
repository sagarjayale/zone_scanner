"""Adding many stocks at once: index members from the NSE Indices file, or pasted symbols."""
from fastapi.testclient import TestClient

from app import settings_store
from app.data import indices
from app.db import SessionLocal
from app.main import app
from app.models import Instrument

FILE = """Company Name,Industry,Symbol,Series,ISIN Code
Alpha Industries Ltd.,Oil Gas & Consumable Fuels,ALPHAIND,EQ,INE0TEST0011
Beta Bank Ltd.,Financial Services,BETABANK,EQ,INE0TEST0029
Gamma Motors Ltd.,Automobile and Auto Components,GAMMAMOT,EQ,INE0TEST0037
Renamed Co Ltd.,Services,OLDNAME,EQ,INE0TEST0045
Missing Co Ltd.,Services,MISSINGCO,EQ,INE0TEST0052
"""


def seed():
    with SessionLocal() as db:
        for sym, isin in (("ALPHAIND", "INE0TEST0011"), ("BETABANK", "INE0TEST0029"), ("GAMMAMOT", "INE0TEST0037"), ("NEWNAME", "INE0TEST0045")):
            db.merge(Instrument(instrument_key=f"NSE_EQ|{isin}", symbol=sym, name=sym.title(), segment="NSE_EQ", instrument_type="EQ"))
        db.commit()
        settings_store.put(db, "universe", ["NSE_EQ|INE0TEST0029"])


def test_parse_and_match():
    seed()
    rows = indices.parse_csv("\ufeff" + FILE)
    assert len(rows) == 5 and rows[0] == {"symbol": "ALPHAIND", "isin": "INE0TEST0011", "name": "Alpha Industries Ltd.", "industry": "Oil Gas & Consumable Fuels"}
    with SessionLocal() as db:
        got = {r["symbol"]: r for r in indices.match(db, rows, {"NSE_EQ|INE0TEST0029"})}
    assert got["ALPHAIND"]["instrument_key"] == "NSE_EQ|INE0TEST0011" and got["ALPHAIND"]["found"] and not got["ALPHAIND"]["in_list"]
    assert got["BETABANK"]["in_list"] is True
    assert got["NEWNAME"]["instrument_key"] == "NSE_EQ|INE0TEST0045"          # matched by ISIN although the symbol changed
    assert got["MISSINGCO"]["found"] is False and got["MISSINGCO"]["instrument_key"] is None
    assert indices.parse_pasted("alphaind, BETABANK\nINE0TEST0037 alphaind") == [
        {"symbol": "ALPHAIND", "isin": ""}, {"symbol": "BETABANK", "isin": ""}, {"isin": "INE0TEST0037", "symbol": ""}]


def test_index_and_paste_apis(monkeypatch):
    seed()

    async def fake_fetch(name):
        if name not in indices.INDICES:
            raise indices.IndexListError("Unknown index.")
        if name == "niftybank":
            raise indices.IndexListError("niftyindices.com did not return the list (status 403). Paste the symbols in the box below instead.")
        return FILE, "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"
    monkeypatch.setattr(indices, "fetch_csv", fake_fetch)
    with TestClient(app) as c:
        res = c.get("/api/instruments/index/nifty50").json()
        assert (res["index"], res["count"]) == ("Nifty 50", 5) and res["source"].endswith("ind_nifty50list.csv")
        assert sum(r["found"] for r in res["rows"]) == 4 and sum(r["in_list"] for r in res["rows"]) == 1
        blocked = c.get("/api/instruments/index/niftybank")
        assert blocked.status_code == 502 and "Paste the symbols" in blocked.json()["detail"]
        assert c.get("/api/instruments/index/nothing").status_code == 502
        pasted = c.post("/api/instruments/match", json={"text": "gammamot, NOPE"}).json()
        assert [(r["symbol"], r["found"]) for r in pasted["rows"]] == [("GAMMAMOT", True), ("NOPE", False)]
        assert c.post("/api/instruments/match", json={"text": "  "}).status_code == 400
