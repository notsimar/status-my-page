"""Coverage-gap round 2: healthcheck CRUD validation branches.

Targets: _validate_host, _clean_healthy_codes, keyword sanitation,
port validation, and the create/update endpoint paths through them.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _csrf(admin):
    return admin.get("/api/csrf-token").get_json()["token"]


class TestCleanHealthyCodes:
    def test_none_returns_none_none(self):
        from statuspage.routes import _clean_healthy_codes
        assert _clean_healthy_codes(None) == (None, None)

    def test_non_list_rejected(self):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes("not-a-list")
        assert codes is None and "array" in err

    def test_numeric_strings_coerced(self):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes(["200", "301"])
        assert codes == [200, 301]

    def test_out_of_range_dropped(self):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes([99, 200, 600])
        assert codes == [200] and err is None

    def test_non_numeric_dropped(self):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes(["abc", 404])
        assert codes == [404] and err is None

    def test_empty_list_means_clear(self):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes([])
        assert codes == [] and err is None


class TestValidateHost:
    def test_missing_host_rejected(self):
        from statuspage.routes import _validate_host
        host, err = _validate_host("")
        assert host is None and "required" in err

    def test_non_string_host_rejected(self):
        from statuspage.routes import _validate_host
        host, err = _validate_host(None)
        assert host is None and "required" in err

    def test_valid_hostname_accepted(self):
        from statuspage.routes import _validate_host
        host, err = _validate_host("db.internal.lan")
        assert host == "db.internal.lan" and err is None

    def test_unsafe_host_rejected(self):
        from statuspage.routes import _validate_host
        host, err = _validate_host("-bad-host-")
        assert host is None and "invalid" in err


class TestKeywordSanitation:
    @pytest.fixture()
    def fn(self):
        from statuspage.routes import _validate_rss_keywords
        return _validate_rss_keywords

    def test_none_ok(self, fn):
        assert fn(None) == (None, None)

    def test_non_dict_rejected(self, fn):
        out, err = fn("red")
        assert out is None and "object" in err

    def test_string_promoted_to_single_item(self, fn):
        out, err = fn({"red": "down"})
        assert out["red"] == ["down"]

    def test_long_words_dropped(self, fn):
        out, err = fn({"red": ["x" * 100, "ok"]})
        assert out["red"] == ["ok"]

    def test_words_lowercased(self, fn):
        out, err = fn({"degraded": ["SLOW"]})
        assert out["degraded"] == ["slow"]

    def test_null_entries_skipped(self, fn):
        out, err = fn({"red": [None, "down", ""]})
        assert out["red"] == ["down"]

    def test_unknown_levels_ignored(self, fn):
        out, err = fn({"red": ["a"], "bogus": ["b"]})
        assert "bogus" not in out


class TestHealthcheckCreateValidation:
    """Create-endpoint validation branches via the API."""

    def _create(self, admin, token, body):
        return admin.post("/api/healthchecks", json=body,
                          headers={"X-CSRF-Token": token})

    def test_ping_missing_host_400(self, admin, token):
        r = self._create(admin, token,
                         {"name": "hc-ping", "type": "ping"})
        assert r.status_code == 400
        assert "host" in r.get_json()["error"]

    def test_ping_invalid_host_400(self, admin, token):
        r = self._create(admin, token,
                         {"name": "hc-ping2", "type": "ping",
                          "host": "-bad-", "interval": 60})
        assert r.status_code == 400

    def test_curl_bad_port_ignored_for_curl_type(self, admin, token):
        """Port validation applies to tcp type; curl ignores it."""
        r = self._create(admin, token,
                         {"name": "hc-curl", "type": "curl",
                          "url": "http://localhost/", "port": "not-int"})
        assert r.status_code in (200, 201)

    def test_tcp_bad_port_400(self, admin, token):
        r = self._create(admin, token,
                         {"name": "hc-tcp-bad-port", "type": "tcp",
                          "host": "127.0.0.1", "port": "not-int"})
        assert r.status_code == 400
        assert "integer" in r.get_json()["error"]

    def test_duplicate_name_conflict(self, admin):
        """Duplicate detection via direct API — each submit gets a fresh
        CSRF token (rotation on success), so no stale-token 403s."""
        body = {"name": "hc-dup2", "type": "curl",
                "url": "http://localhost/a"}
        r1 = self._create(admin, self._csrf(admin), body)
        assert r1.status_code in (200, 201)
        r2 = self._create(admin, self._csrf(admin), body)
        assert r2.status_code == 409

    @staticmethod
    def _csrf(admin):
        return admin.get("/api/csrf-token").get_json()["token"]

    def _create(self, admin, token, body):
        return admin.post("/api/healthchecks", json=body,
                          headers={"X-CSRF-Token": token})

    def test_tcp_with_port_200(self, admin, token, monkeypatch):
        monkeypatch.setattr("statuspage.healthcheck._MODULE_CONFIGURED", True)
        r = self._create(admin, token,
                         {"name": "hc-tcp", "type": "tcp",
                          "host": "127.0.0.1", "port": 5432})
        assert r.status_code in (200, 201), r.get_json()


class TestSlackMaxQueueFallback:
    def test_malformed_max_queue_falls_back(self, monkeypatch):
        from statuspage import slack as slack_mod
        monkeypatch.setattr(slack_mod, "load_config", lambda: {
            "slack": {"enabled": True, "max_queue": "garbage"}})
        conf = slack_mod.get_slack_config()
        assert isinstance(conf["max_queue"], int)
        assert conf["max_queue"] >= 1

    def test_max_queue_clamped_to_upper_bound(self, monkeypatch):
        from statuspage import slack as slack_mod
        monkeypatch.setattr(slack_mod, "load_config", lambda: {
            "slack": {"enabled": True, "max_queue": 999999}})
        conf = slack_mod.get_slack_config()
        assert conf["max_queue"] <= 5000

    def test_is_slack_enabled_requires_both(self, monkeypatch):
        from statuspage import slack as slack_mod
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": True, "webhook_url": "", "channel": "", "max_queue": 10})
        assert slack_mod.is_slack_enabled() is False
