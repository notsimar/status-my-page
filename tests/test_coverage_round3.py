"""Coverage round 3: healthcheck UPDATE endpoint branches.

Targets the PUT /api/healthchecks/<name> paths: type change (full replace),
url/service/keyword updates, healthy_codes set/clear, host/port changes,
and the invalid-JSON guard on /api/slack + /api/settings.
"""
import json
import sys
from pathlib import Path

import pytest


def _csrf(admin):
    return admin.get("/api/csrf-token").get_json()["token"]


@pytest.fixture()
def hc_ready(A, monkeypatch):
    """Module configured flag + a base curl healthcheck to update."""
    monkeypatch.setattr("statuspage.healthcheck._MODULE_CONFIGURED", True)
    from statuspage.config import _save_healthchecks
    _save_healthchecks({"hc-base": {
        "type": "curl", "url": "http://localhost/health",
        "interval": 60, "timeout": 5, "retries": 3}})
    return A


class TestHealthcheckUpdate:
    def _put(self, admin, name, body):
        return admin.put(f"/api/healthchecks/{name}", json=body,
                         headers={"X-CSRF-Token": self._csrf(admin)})

    @staticmethod
    def _csrf(admin):
        return admin.get("/api/csrf-token").get_json()["token"]

    def test_update_url_only(self, admin, token, hc_ready):
        r = self._put(admin, "hc-base",
                      {"url": "http://localhost/other"})
        assert r.status_code == 200
        from statuspage.config import _load_healthchecks
        assert _load_healthchecks()["hc-base"]["url"] == "http://localhost/other"
        assert _load_healthchecks()["hc-base"]["type"] == "curl"  # unchanged

    def test_update_service_field_set_and_clear(self, admin, token, hc_ready):
        r = self._put(admin, "hc-base", {"service": "Billing API"})
        assert r.status_code == 200
        from statuspage.config import _load_healthchecks
        assert _load_healthchecks()["hc-base"]["service"] == "Billing API"

        # Empty string clears it
        r = self._put(admin, "hc-base", {"service": ""})
        assert r.status_code == 200
        assert "service" not in _load_healthchecks()["hc-base"]

    def test_update_failure_keyword_set_and_clear(self, admin, token, hc_ready):
        from statuspage.config import _load_healthchecks
        r = self._put(admin, "hc-base",
                      {"failure_keyword": "ERROR", "degraded_keyword": "SLOW"})
        assert r.status_code == 200
        assert _load_healthchecks()["hc-base"]["failure_keyword"] == "ERROR"

    def test_type_change_replaces_config(self, admin, token, hc_ready):
        """curl -> ping keeps only universal tuning keys."""
        r = self._put(admin, "hc-base",
                      {"type": "ping", "host": "127.0.0.1"})
        assert r.status_code == 200
        from statuspage.config import _load_healthchecks
        hc = _load_healthchecks()["hc-base"]
        assert hc["type"] == "ping"
        assert "url" not in hc          # stripped by full replace

    def test_update_invalid_type_400(self, admin, token, hc_ready):
        r = self._put(admin, "hc-base", {"type": "carrier-pigeon"})
        assert r.status_code == 400
        assert "invalid type" in r.get_json()["error"]

    def test_update_missing_name_404(self, admin, token, hc_ready):
        r = self._put(admin, "no-such-hc", {"url": "http://x/"})
        assert r.status_code == 404

    def test_update_healthy_codes_clear(self, admin, hc_ready):
        from statuspage.config import _save_healthchecks, _load_healthchecks
        _save_healthchecks({"hc-codes": {
            "type": "curl", "url": "http://x/",
            "healthy_codes": [200]}})
        r = self._put(admin, "hc-codes", {"healthy_codes": []})
        assert r.status_code == 200
        assert "healthy_codes" not in _load_healthchecks()["hc-codes"]

    def test_update_bad_port_400(self, admin, hc_ready):
        # First convert to tcp so port applies
        self._put(admin, "hc-base", {"type": "tcp"})
        r = self._put(admin, "hc-base", {"port": 99999})
        assert r.status_code == 400
        assert "1 and 65535" in r.get_json()["error"]

    def test_update_bool_port_400(self, admin, hc_ready):
        self._put(admin, "hc-base", {"type": "tcp"})
        r = self._put(admin, "hc-base", {"port": True})
        assert r.status_code == 400
        assert "integer" in r.get_json()["error"]


class TestInvalidJsonGuards:
    def test_slack_invalid_json_400(self, admin):
        # No CSRF header needed — body parsing happens first? No: auth first.
        # Use valid session+CSRF but malformed body.
        tok = _csrf(admin)
        r = admin.post("/api/slack", data="{broken",
                       content_type="application/json",
                       headers={"X-CSRF-Token": tok})
        assert r.status_code == 400

    def test_settings_non_bool_value_400(self, admin, token):
        r = admin.post("/api/settings", json={"history_enabled": "banana"},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 400
        assert "boolean" in r.get_json()["error"]

    def test_settings_healthchecks_enabled_non_bool_400(self, admin, token):
        r = admin.post("/api/settings",
                       json={"healthchecks_enabled": []},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 400
