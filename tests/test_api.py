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
        assert len([r for r in app.routes if getattr(r, "tags", None)]) == 47   # auth 3, data 7, scan 9, system 7, scan-date checks 4, replay 1, backtest 5, live 2, paper 7, options 2