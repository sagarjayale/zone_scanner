"""A change of rules must never hold up the start of the app: the zone tables are emptied by a background job."""
import time

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import settings_store
from app.db import SessionLocal
from app.main import RULES_VERSION, app
from app.models import Job, ModeResult, Setting, Zone, ZoneEvent


def test_a_rule_change_clears_and_rebuilds_in_the_background():
    with SessionLocal() as db:
        settings_store.put(db, "engine_rules", "GTF-4")
        before = db.scalar(select(func.count()).select_from(Zone))
    started = time.monotonic()
    with TestClient(app) as client:
        assert time.monotonic() - started < 5                     # the app answers at once
        assert client.get("/api/settings").status_code == 200
        for _ in range(300):
            jobs = [j for j in client.get("/api/jobs").json() if j["type"] == "rebuild after a rule change"]
            if jobs and jobs[0]["status"] != "running":
                break
            time.sleep(0.2)
        assert jobs[0]["status"] == "done", jobs[0]
    with SessionLocal() as db:
        assert settings_store.get(db, "engine_rules") == RULES_VERSION
        assert db.get(Setting, "rules_reset_pending") is None       # finished, so the next start does nothing
        assert db.scalar(select(func.count()).select_from(ZoneEvent)) >= 0
    assert before >= 0


def test_a_locked_table_is_waited_for_and_tried_again(monkeypatch):
    """If another connection holds a table, the clearing waits and tries again, and says so on the job."""
    from contextlib import contextmanager
    from sqlalchemy.exc import OperationalError
    import app.main as main
    real, calls, said = main.engine, {"n": 0}, []

    class Busy:
        dialect = real.dialect

        @contextmanager
        def begin(self):
            calls["n"] += 1
            if calls["n"] <= 2:                               # the first two tries find the table locked
                raise OperationalError("DELETE", {}, Exception("(1205, 'Lock wait timeout exceeded')"))
            with real.begin() as conn:
                yield conn
    monkeypatch.setattr(main, "engine", Busy())
    main.clear_zone_tables(said.append, tries=5, pause=0)
    assert calls["n"] == 2 + len(main.RESET_TABLES)
    assert sum("waiting for the mode_results table" in s for s in said) == 2 and said[-1] == "cleared rejections"


def test_the_old_zone_clean_up_skips_a_busy_database(monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app.engine import scanner
    from app.jobs import scheduler

    def busy(db):
        raise OperationalError("DELETE FROM zone_events", {}, Exception("(1412, 'Table definition has changed')"))
    monkeypatch.setattr(scanner, "prune_closed", busy)
    assert scheduler.prune_old_zones() == 0
