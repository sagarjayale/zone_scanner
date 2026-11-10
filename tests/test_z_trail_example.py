"""The lock defaults and the example on the Settings page: buy 100, stop 95, target 110, step 10 %, gap 20 %, live.
The page example (node) and the real trailing stop (sim/trailing.py) must tell the same story."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULTS
from app.main import app
from app.sim import trailing

NODE = shutil.which("node") or "/opt/node22/bin/node"
JS = Path(__file__).parent.parent / "app" / "web" / "js"


def _node(script: str):
    if not Path(NODE).exists():
        pytest.skip("node is not installed")
    out = subprocess.run([NODE, "-e", (JS / "trailexample.js").read_text() + "\n" + script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_page_defaults_are_the_app_defaults():
    got = _node("console.log(JSON.stringify(TRAIL_DEFAULTS))")
    assert got == {k: DEFAULTS[k] for k in got}
    assert (DEFAULTS["trail_method"], DEFAULTS["trail_tf"], DEFAULTS["trail_lock_pct"], DEFAULTS["trail_lock_gap_pct"],
            DEFAULTS["trail_lock_behind"], DEFAULTS["trail_lock_target"]) == ("lock", "live", 10.0, 20.0, False, "move")


def test_the_example_with_the_defaults():
    e = _node("console.log(JSON.stringify(lockExample({ pct: 10, gapPct: 20, live: true, move: true })))")
    assert (e["step"], e["gap"]) == (1, 0.2)
    assert e["fallBack"] == [["101", "95", "not past 101.2 yet"], ["101.2", "101 🔒", "step 1, locked at once (live)"],
                             ["100.9", "—", "closed at 101, +0.2 R"]]
    assert e["up"] == [["102.2", "102 🔒", "step 2"], ["105.2", "105 🔒", "steps 3 to 5 lock together"],
                       ["110", "", "target moves to 111, trade stays open"], ["111.2", "111 🔒", "step 11 (1 beyond the target)"],
                       ["110.9", "—", "closed at 111, +2.2 R"]]
    behind = _node("console.log(JSON.stringify(lockExample({ pct: 10, gapPct: 20, live: true, move: true, behind: true })))")
    assert behind["fallBack"][1][1].startswith("100 + charges") and behind["up"][1][1] == "104 🔒"


def test_the_real_trailing_stop_does_the_same():
    opts = trailing.options(DEFAULTS)
    t = trailing.Trailer("demand", 100, 95, opts, target=110)
    assert t.see_price(101.0) is None and t.stop == 95
    assert t.see_price(101.2) == pytest.approx(101)
    assert t.stop > 100.9                                  # a price of 100.9 hits the stop: closed at 101, +0.2 R
    t = trailing.Trailer("demand", 100, 95, opts, target=110)
    for price, stop in ((102.2, 102), (105.2, 105)):
        t.see_price(price)
        assert t.stop == pytest.approx(stop)
    t.see_price(110)
    assert t.target == pytest.approx(111) and t.live_target() == float("inf")    # the target moves, the trade stays open
    t.see_price(111.2)
    assert t.stop == pytest.approx(111) and (t.stop - 100) / 5 == pytest.approx(2.2)


def test_settings_page_shows_the_example_and_the_defaults_button():
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert 'id="trail-example"' in page and 'id="trail-defaults"' in page and "/static/js/trailexample.js" in page
        js = c.get("/static/js/settings.js").text
        assert "lockExampleBox(" in js and "TRAIL_DEFAULTS" in js
