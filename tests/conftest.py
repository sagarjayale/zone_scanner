import os
import tempfile

# Tests run on a throwaway SQLite file by default. To run them on MySQL instead, set
# TEST_DATABASE_URL, for example mysql+pymysql://user:pass@localhost:3306/scanner_test
# (URL-encode special characters in the password here: @ becomes %40).
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["UPSTOX_ACCESS_TOKEN"] = ""
os.environ["SCHEDULER_ENABLED"] = "0"   # no schedule and no price polling during tests

import app.models  # noqa: E402,F401  registers the tables
from app.db import Base, engine, ensure_database  # noqa: E402

ensure_database()
Base.metadata.drop_all(engine)   # the test database starts empty every run
Base.metadata.create_all(engine)

from app import settings_store  # noqa: E402
from app.db import SessionLocal  # noqa: E402

with SessionLocal() as _db:      # the test database starts on the current rules, so nothing is cleared at start-up
    settings_store.put(_db, "engine_rules", "GTF-5")
    # Most tests were written for the course (PDF) marking with all history; the scanner.py marking has its own tests
    # (test_scanner_rules.py), which switch to it where they need the database.
    settings_store.put(_db, "marking_rule", "pdf")
    settings_store.put(_db, "zone_history", "all")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _trailing_off_for_older_tests(request):
    """The trailing stop is on by default (lock, on live prices). Most tests were written with it off and check one
    rule at a time, so each test starts with it off; tests marked trailing_default see the real defaults."""
    with SessionLocal() as db:
        if request.node.get_closest_marker("trailing_default"):
            from app.models import Setting
            row = db.get(Setting, "trail_method")
            if row is not None:
                db.delete(row)
                db.commit()
        else:
            settings_store.put(db, "trail_method", "off")
    yield


def pytest_configure(config):
    config.addinivalue_line("markers", "trailing_default: run with the real trailing defaults (lock on live prices)")
