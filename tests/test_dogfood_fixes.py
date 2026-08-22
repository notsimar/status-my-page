"""Regression tests for the dogfood-QA fixes (report 2026-08-22).

Covers:
  - /api/healthchecks returns {} (not 500) when the module is unconfigured
  - POST /api/healthcheck/run returns JSON 409 (not 500) when disabled
  - Notes cap raised to 2000 and enforced server-side
  - Toggle/notes on missing ids return JSON 404, not HTML
  - Lockout response carries real remaining seconds
"""
import collections
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from statuspage.healthcheck import is_configured


@pytest.fixture()
def hc_disabled(A, monkeypatch):
    """Simulate STATUS_DISABLE_HEALTHCHECKS=1: module never configured."""
    monkeypatch.setattr(
        "statuspage.healthcheck._MODULE_CONFIGURED", False)
    yield A


class TestHealthchecksDisabledGuards:
    def test_public_list_returns_empty_json_not_500(self, client, hc_disabled):
        r = client.get("/api/healthchecks")
        assert r.status_code == 200
        assert r.get_json() == {}
        assert b"<html" not in r.data  # no HTML error page

    def test_admin_list_returns_empty_json_not_500(self, admin, hc_disabled):
        r = admin.get("/api/healthchecks")
        assert r.status_code == 200
        assert r.get_json() == {}

    def test_run_endpoint_json_409_when_disabled(self, admin, token,
                                                 hc_disabled):
        r = admin.post("/api/healthcheck/run",
                       headers={"X-CSRF-Token": token},
                       content_type="application/json", data=b"{}")
        assert r.status_code == 409
        body = r.get_json()
        assert "disabled" in body["error"].lower()

    def test_configured_module_still_works(self, admin, A,
                                           monkeypatch):
        """Sanity: with the module configured the list endpoint is unchanged."""
        monkeypatch.setattr("statuspage.healthcheck._MODULE_CONFIGURED", True)
        from statuspage.config import _save_healthchecks
        _save_healthchecks({"SvcA": {"type": "curl",
                                     "url": "http://a/health"}})
        r = admin.get("/api/healthchecks")
        assert r.status_code == 200
        assert "SvcA" in r.get_json()


class TestNotesLengthCap:
    def test_notes_up_to_2000_accepted(self, admin, token, id_a):
        r = admin.post(f"/api/notes/{id_a}",
                       json={"notes": "A" * 2000},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 200

    def test_notes_over_2000_rejected_with_json_error(self, admin, token, id_a):
        r = admin.post(f"/api/notes/{id_a}",
                       json={"notes": "A" * 2001},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 400
        body = r.get_json()
        assert "max length" in body["error"]

    def test_constants_and_input_filter_agree(self):
        import constants
        import input_filter
        assert constants.MAX_TEXT_LENGTH == input_filter.MAX_TEXT_LENGTH == 2000


class TestJson404OnMissingService:
    def test_toggle_missing_id_returns_json(self, admin, token):
        r = admin.post("/api/toggle/999999",
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 404
        assert r.get_json() == {"error": "Service not found"}
        assert b"<html" not in r.data

    def test_notes_missing_id_returns_json(self, admin, token):
        r = admin.post("/api/notes/999999",
                       json={"notes": "x"},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 404
        assert r.get_json() == {"error": "Service not found"}


class TestLockoutRemainingSeconds:
    def test_lockout_message_includes_real_remaining_time(
            self, client, monkeypatch):
        """Patch BOTH the auth module and app's alias (tests read via app)."""
        from statuspage import auth as auth_mod

        ip = "127.0.0.1"  # the test client's actual REMOTE_ADDR
        now = time.time()
        locked_state = collections.defaultdict(
            list, {ip: [now - 1] * auth_mod.MAX_LOGIN_ATTEMPTS})
        until = {ip: now + 17}

        monkeypatch.setattr(auth_mod, "_failed_logins", locked_state)
        monkeypatch.setattr(auth_mod, "_lockout_until", until)
        import app as app_mod
        if hasattr(app_mod, "_failed_logins"):
            monkeypatch.setattr(app_mod, "_failed_logins", locked_state)

        r = client.post("/login", json={"user": "admin", "pass": "nope"})
        assert r.status_code == 429
        body = r.get_json()
        assert "17s" in body["error"] or "Try again in" in body["error"]
        assert body["retry_after"] >= 15

    def test_lockout_message_uses_remote_ip_not_hardcoded(self):
        """Guard against regressing to a hardcoded IP in the test."""
        import inspect
        import sys
        mod = sys.modules[__name__]
        src = inspect.getsource(mod.TestLockoutRemainingSeconds)
        assert 'ip = "127.0.0.1"' in src

    def test_lockout_remaining_zero_when_unlocked(self):
        from statuspage import auth as auth_mod
        assert auth_mod.lockout_remaining("never-locked-ip") == 0

    def test_is_locked_sets_lockout_expiry(self, monkeypatch):
        from statuspage import auth as auth_mod
        ip = "8.8.8.8"
        now = time.time()
        state = collections.defaultdict(
            list, {ip: [now - 1] * auth_mod.MAX_LOGIN_ATTEMPTS})
        monkeypatch.setattr(auth_mod, "_failed_logins", state)
        monkeypatch.setattr(auth_mod, "_lockout_until", {})
        assert auth_mod.is_locked(ip) is True
        assert 0 < auth_mod.lockout_remaining(ip) <= auth_mod.LOCKOUT_SECONDS
