from fastapi.testclient import TestClient

from app.main import app


def test_api_boots_and_reports_empty_state():
    with TestClient(app) as c:
        assert c.get("/api/auth/status").json()["has_token"] is False
        assert "rows" in c.get("/api/data/status").json()
        s = c.get("/api/settings").json()
        assert s["min_score"] == 5.0 and "access_token" not in s
        assert c.put("/api/settings", json={"min_score": 7}).json()["min_score"] == 7
        c.put("/api/settings", json={"min_score": 5.0})
        assert s["direction_rule"] == "location" and c.put("/api/settings", json={"direction_rule": "sometimes"}).status_code == 400
        assert c.put("/api/settings", json={"direction_rule": "trend"}).json()["direction_rule"] == "trend"
        c.put("/api/settings", json={"direction_rule": "location"})
        assert c.put("/api/settings", json={"access_token": "x"}).status_code == 400
        assert c.get("/api/setups").status_code == 200
        assert c.get("/api/zone/999999").status_code == 404
        assert c.post("/api/data/sync/NSE_INDEX|Nifty 50").status_code == 401   # no token yet
        assert len([r for r in app.routes if getattr(r, "tags", None)]) == 65   # paper apply trailing 1, paper auto run 1, auth 3, data 9 (with /api/chart and /api/forming), scan 10 (with /api/eye), system 7, scan-date checks 5 (with export), replay 1, backtest 6 (with delete), live 3, paper 10 (with export), options 2, option paper 7