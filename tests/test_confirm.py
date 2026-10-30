"""Confirmation score: the pieces that need no database."""
from app.engine import confirm


def test_mode_agreement_points():
    a = confirm.agreement("BUY", [("WEEKLY", "BUY"), ("MONTHLY", "BUY"), ("QUARTERLY", "NO_TRADE")])
    assert (a["points"], a["max"], a["note"]) == (2, 2, "Weekly, Monthly agree")            # a mode with no trade is not counted
    a = confirm.agreement("BUY", [("WEEKLY", "BUY"), ("MONTHLY", "SELL")])
    assert (a["points"], a["passed"]) == (1, False) and "Monthly against" in a["note"]      # half agree
    assert confirm.agreement("BUY", [("WEEKLY", "SELL"), ("MONTHLY", "SELL"), ("YEARLY", "BUY")])["points"] == 0
    none = confirm.agreement("SELL", [("WEEKLY", "NO_TRADE")])
    assert (none["applicable"], none["points"], none["max"]) == (False, 0, 0)               # nothing to compare with: left out


def test_summary_counts_only_what_could_be_checked():
    checks = [confirm.check("a", "A", 2, 2, ""), confirm.check("b", "B", 1, 1, ""), confirm.check("c", "C", 0, 1, ""),
              confirm.check("d", "D", 0, 1, "", applicable=False)]
    s = confirm.summary(checks)
    assert (s["points"], s["max"], s["pct"], s["label"]) == (3, 4, 75, "strong")
    assert confirm.summary([confirm.check("a", "A", 1, 2, "")])["label"] == "moderate"
    assert confirm.summary([confirm.check("a", "A", 0, 2, "")])["label"] == "weak"
    assert confirm.summary([confirm.check("a", "A", 0, 1, "", applicable=False)]) == {"points": 0, "max": 0, "pct": None, "label": None, "checks": [confirm.check("a", "A", 0, 1, "", applicable=False)]}


def test_buildup_names():
    assert confirm.buildup(62, 55, 4000) == "long build-up" and confirm.buildup(62, 55, -4000) == "short covering"
    assert confirm.buildup(50, 55, 4000) == "short build-up" and confirm.buildup(50, 55, -4000) == "long unwinding"
    assert confirm.buildup(55, 55, 100) is None and confirm.buildup(62, None, 100) is None


def test_premium_estimate_and_option_checks():
    call = {"ltp": 62.0, "delta": 0.55, "gamma": 0.002, "close": 55.0, "oi_change": 4000, "instrument_key": "X"}
    spot = 3480.0
    at = {k: confirm.premium_at(call, spot, v) for k, v in (("entry", 3455), ("stop", 3430), ("target", 3505))}
    assert at == {"entry": 48.88, "stop": 37.0, "target": 76.38}          # 62 - 0.55*25 + 0.001*625, and so on
    put = {"ltp": 40.0, "delta": -0.45, "gamma": 0.002}
    assert confirm.premium_at(put, spot, 3455) > 40 > confirm.premium_at(put, spot, 3505)      # a put gains when the stock falls
    assert confirm.premium_at({"ltp": 2.0, "delta": 0.3, "gamma": 0}, spot, 3400) == 0.05      # never below the smallest tick
    assert confirm.premium_at({"ltp": 62.0, "delta": None}, spot, 3455) is None
    levels = {"rr": round((76.38 - 48.88) / (48.88 - 37.0), 2)}
    view = {"call_wall": 3500, "put_wall": 3440, "pcr": 1.08}
    got = {c["key"]: c for c in confirm.option_checks("BUY", call, levels, view, {"percentile": 38}, 19, 3455, 3505)}
    assert [got[k]["points"] for k in ("buildup", "wall", "pcr", "iv", "expiry", "premium_rr")] == [1, 0, 1, 1, 1, 1]
    assert "between the entry and the target" in got["wall"]["note"]         # 3,500 sits before the 3,505 target
    clear = {c["key"]: c for c in confirm.option_checks("BUY", call, levels, {**view, "call_wall": 3520}, {"percentile": 85}, 3, 3455, 3505)}
    assert (clear["wall"]["points"], clear["iv"]["points"], clear["expiry"]["points"]) == (1, 0, 0)
    sell = {c["key"]: c for c in confirm.option_checks("SELL", None, None, {"call_wall": 3500, "put_wall": 3440, "pcr": 1.08}, {"percentile": None, "days": 4, "needed": 20}, None, 3455, 3405)}
    assert sell["wall"]["points"] == 0 and sell["pcr"]["points"] == 0        # the put wall at 3,440 is between 3,455 and 3,405
    assert [sell[k]["applicable"] for k in ("buildup", "iv", "expiry", "premium_rr")] == [False] * 4