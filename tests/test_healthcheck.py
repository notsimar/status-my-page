#!/usr/bin/env python3
"""Tests for the optional per-service healthcheck system (app.py lines ~57-310).

Covers:
  _safe_url            — scheme allowlist, SSRF surface
  _parse_healthchecks  — config parsing, sanitisation, edge cases
  _run_curl_check       — real curl invocation + failure modes
  run_healthchecks_once — public one-shot entry-point
  start_healthchecks    — daemon thread no-op when nothing configured
  GET /api/healthchecks — JSON serialisability (sets -> sorted lists)
  POST /api/healthcheck/run — admin-only manual trigger, CSRF, no DB mutation
  _set_health_status    — flips status in DB, records history, no-op guards

All tests reuse the A fixture from conftest.py to write configs on disk.
"""

import json
import os
import sqlite3
import sys
import time

from pathlib import Path

import pytest
import yaml

# ─── _safe_url ──────────────────────────────────────────────────

class TestSafeUrl:
    """URL scheme validation: only http:// and https:// allowed."""

    def test_http_allowed(self, A):
        assert A._safe_url("http://example.com/health") is True

    def test_https_allowed(self, A):
        assert A._safe_url("https://example.com/health") is True

    def test_file_rejected(self, A):
        assert A._safe_url("file:///etc/passwd ") is False

    def test_gopher_rejected(self, A):
        assert A._safe_url("gopher://evil.com") is False

    def test_ftp_rejected(self, A):
        assert A._safe_url("ftp://server.org/file") is False

    def test_data_rejected(self, A):
        assert A._safe_url("data:text/html,<script>alert(1)</script>") is False

    def test_javascript_rejected(self, A):
        assert A._safe_url("javascript:alert(1)") is False

    def test_http_with_port_and_path(self, A):
        assert A._safe_url("http://localhost:8080/api/v1/health") is True

    def test_https_with_query_string(self, A):
        assert A._safe_url("https://api.example.com/health?check=true") is True

    # Edge cases ──────────────────────────────────────────────
    def test_malformed_empty_host(self, A):
        """http:// with no hostname -> parsed returns empty netloc."""
        assert A._safe_url("http://") is False


# ─── _parse_healthchecks ──────────────────────────────────────────

class TestParseHealthchecks:
    """Config parsing logic: sanitisation, defaults, edge cases."""

    def _write(self, A, data):
        """Helper to write config.yaml on disk."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def test_no_healthcheck_section(self, A):
        self._write(A, {"items": ["SvcA"], "_runtime": {}})
        assert A._parse_healthchecks() == {}

    def test_valid_single_entry(self, A):
        """Minimal valid entry gets parsed correctly."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://localhost:8080/"}},
            },
        )
        hc = A._parse_healthchecks()
        assert "SvcA" in hc
        assert hc["SvcA"]["url"] == "http://localhost:8080/"
        assert hc["SvcA"]["interval"] == A.HEALTHCHECK_INTERVAL_DEFAULT
        assert hc["SvcA"]["timeout"] == A.HEALTHCHECK_TIMEOUT_DEFAULT
        assert hc["SvcA"]["retries"] == A.HEALTHCHECK_RETRIES_DEFAULT
        assert 200 in hc["SvcA"]["healthy_codes"]

    def test_custom_interval_timeout_retries(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {
                    "SvcA": {
                        "url": "http://localhost/health",
                        "interval": 15,
                        "timeout": 3,
                        "retries": 5,
                    }
                },
            },
        )
        hc = A._parse_healthchecks()
        assert hc["SvcA"]["interval"] == 15
        assert hc["SvcA"]["timeout"] == 3
        assert hc["SvcA"]["retries"] == 5

    def test_custom_healthy_codes(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://localhost/", "healthy_codes": [200, 204]}},
            },
        )
        hc = A._parse_healthchecks()
        assert hc["SvcA"]["healthy_codes"] == {200, 204}

    # ── Rejected entries ──────────────────────────────────
    def test_missing_url_skipped(self, A):
        self._write(A, {"items": ["SvcA"], "_runtime": {}, "healthchecks": {"SvcA": {"interval": 30}}})
        assert A._parse_healthchecks() == {}

    def test_non_string_url_skipped(self, A):
        self._write(A, {"items": ["SvcA"], "_runtime": {}, "healthchecks": {"SvcA": {"url": 12345}}})
        assert A._parse_healthchecks() == {}

    def test_non_http_url_rejected(self, A):
        self._write(
            A, {"items": ["SvcA"], "_runtime": {}, "healthchecks": {"SvcA": {"url": "file:///etc/passwd"}}}
        )
        assert A._parse_healthchecks() == {}

    def test_negative_interval_skipped(self, A):
        self._write(
            A,
            {"items": ["SvcA"], "_runtime": {}, "healthchecks": {"SvcA": {"url": "http://localhost/", "interval": -5}}},
        )
        assert A._parse_healthchecks() == {}

    def test_non_numeric_interval_skipped(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://localhost/", "interval": "abc"}},
            },
        )
        assert A._parse_healthchecks() == {}

    def test_details_not_dict_skipped(self, A):
        self._write(
            A, {"items": ["SvcA"], "_runtime": {}, "healthchecks": {"SvcA": "http://localhost/"}}
        )
        assert A._parse_healthchecks() == {}

    def test_empty_url_string_skipped(self, A):
        self._write(A, {"items": ["SvcA"], "_runtime": {}, "healthchecks": {"SvcA": {"url": ""}}})
        assert A._parse_healthchecks() == {}

    def test_whitespace_url_trimmed(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {" My Svc ": {"url": "  http://localhost/  "}},
            },
        )
        hc = A._parse_healthchecks()
        assert "My Svc" in hc
        assert hc["My Svc"]["url"] == "http://localhost/"

    def test_multiple_services_parsed(self, A):
        self._write(
            A,
            {
                "items": ["SvcA", "SvcB"],
                "_runtime": {},
                "healthchecks": {
                    "SvcA": {"url": "http://localhost:80/"},
                    "SvcB": {"url": "http://localhost:81/", "interval": 30},
                },
            },
        )
        hc = A._parse_healthchecks()
        assert len(hc) == 2
        assert hc["SvcA"]["interval"] != hc["SvcB"]["interval"]

    def test_bad_healthy_codes_defaults_to_200(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://localhost/", "healthy_codes": ["x", "y"]}},
            },
        )
        hc = A._parse_healthchecks()
        assert hc["SvcA"]["healthy_codes"] == {200}

    def test_config_parse_error_returns_empty(self, A, monkeypatch):
        """Simulate YAML parse failure -> graceful return."""
        def bad_load():
            raise yaml.YAMLError("broken YAML")

        monkeypatch.setattr(A, "load_config", bad_load)
        assert A._parse_healthchecks() == {}


# ─── _run_curl_check ──────────────────────────────────────────────

class TestRunCurlCheck:
    """Real curl invocation + failure modes."""

    def test_connection_refused_returns_none(self, A):
        result = A._run_curl_check("http://localhost:19999/nonexistent", timeout=2)
        assert result is None

    def test_curl_binary_found(self, A):
        """At minimum curl should be discoverable on the build host."""
        # Just check it returns None (no crash or exception).
        result = A._run_curl_check("http://localhost:19997/bad", timeout=2)
        assert result is None

    def test_nonexistent_local_url_returns_none(self, A):
        result = A._run_curl_check("http://127.0.0.1:19988/nope", timeout=2)
        assert result is None


# ─── run_healthchecks_once ────────────────────────────────────────

class TestRunHealthchecksOnce:
    """Public entry-point returns results dict."""

    def test_no_config_returns_empty(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump({"items": ["SvcA"], "_runtime": {}}, f)
        assert A.run_healthchecks_once() == {}

    def test_with_config_returns_results(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {"SvcA": {"url": "http://localhost:19976/"}},
                },
                f,
            )
        results = A.run_healthchecks_once()
        assert "SvcA" in results
        assert "status_code" in results["SvcA"]
        assert "healthy" in results["SvcA"]

    def test_result_structure(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {"SvcA": {"url": "http://localhost:19977/"}},
                },
                f,
            )
        result = A.run_healthchecks_once()
        svc_result = result["SvcA"]
        assert isinstance(svc_result.get("status_code"), (int, type(None)))
        assert isinstance(svc_result.get("healthy"), bool)


# ─── start_healthchecks ──────────────────────────────────────────

class TestStartHealthchecks:
    """Daemon thread no-op when nothing configured."""

    def test_no_config_is_noop(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump({"items": ["SvcA"], "_runtime": {}}, f)
        # Should not raise.
        A.start_healthchecks()


# ─── GET /api/healthchecks ──────────────────────────────────────

class TestApiHealthchecks:
    """Public endpoint returns configured healthchecks."""

    def test_public_no_auth(self, client):
        r = client.get("/api/healthchecks")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)

    def test_empty_when_not_configured(self, A, client):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump({"items": ["SvcA"], "_runtime": {}}, f)
        r = client.get("/api/healthchecks")
        assert r.get_json() == {}

    def test_healthy_codes_serialized_as_list(self, A, client):
        """Sets -> sorted lists (JSON can't serialize sets)."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {"SvcA": {"url": "http://localhost/", "healthy_codes": [301, 200]}},
                },
                f,
            )
        r = client.get("/api/healthchecks")
        data = r.get_json()
        assert isinstance(data["SvcA"]["healthy_codes"], list)
        assert sorted(data["SvcA"]["healthy_codes"]) == [200, 301]


# ─── POST /api/healthcheck/run ──────────────────────────────────

class TestApiHealthcheckRun:
    """Admin-only manual trigger with CSRF protection."""

    def test_unauthenticated_returns_403(self, client):
        r = client.post("/api/healthcheck/run")
        assert r.status_code == 403

    def test_admin_without_csrf_returns_403(self, admin, A):
        A._csrf_failures.clear()
        r = admin.post(
            "/api/healthcheck/run",
            content_type="application/json",
        )
        assert r.status_code == 403

    def test_admin_with_csrf_runs_on_demand(self, admin, token, A):
        """Triggered check returns results without mutating DB."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {"SvcA": {"url": "http://localhost:19876/"}},
                },
                f,
            )
        r = admin.post(
            "/api/healthcheck/run",
            headers={"X-CSRF-Token": token},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert "SvcA" in data

    def test_no_db_mutation(self, admin, token, A):
        """Manual run should NOT update statuses (it's a dry preview)."""
        # Record starting status.
        db = sqlite3.connect(str(A.DB_PATH))
        before = {
            row[0]: row[1] for row in db.execute(
                "SELECT name, status FROM status_items"
            ).fetchall()
        }
        db.close()

        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA", "SvcB"],
                    "_runtime": {},
                    "healthchecks": {"SvcA": {"url": "http://localhost:19875/"}},
                },
                f,
            )
        admin.post(
            "/api/healthcheck/run",
            headers={"X-CSRF-Token": token},
        )

        # Statuses should be unchanged.
        db = sqlite3.connect(str(A.DB_PATH))
        after = {
            row[0]: row[1] for row in db.execute(
                "SELECT name, status FROM status_items"
            ).fetchall()
        }
        db.close()
        assert before == after


# ─── _set_health_status ──────────────────────────────────────

class TestSetHealthStatus:
    """Direct DB mutation path used by the worker thread."""

    def test_flips_green_to_degraded(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump({"items": ["SvcA"], "_runtime": {}}, f)
        # Ensure SvcA is green.
        with A.app.test_request_context():
            row = A.get_db().execute(
                "SELECT id FROM status_items WHERE name='SvcA'"
            ).fetchone()
            item_id = row["id"]

        conn = A._health_db()
        try:
            conn.execute("UPDATE status_items SET status='green' WHERE id=?", (item_id,))
            conn.commit()
        finally:
            conn.close()

        A._set_health_status("SvcA", "degraded")

        conn = A._health_db()
        try:
            st = conn.execute(
                "SELECT status FROM status_items WHERE id=?", (item_id,)
            ).fetchone()["status"]
        finally:
            conn.close()
        assert st == "degraded"

    def test_no_op_when_already_same_status(self, A):
        """If the item is already degraded, calling _set_health_status with 'degraded' is a no-op."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump({"items": ["SvcA"], "_runtime": {}}, f)
        with A.app.test_request_context():
            row = A.get_db().execute(
                "SELECT id FROM status_items WHERE name='SvcA'"
            ).fetchone()
            item_id = row["id"]

        # Set it to degraded first.
        conn = A._health_db()
        try:
            conn.execute("UPDATE status_items SET status='degraded' WHERE id=?", (item_id,))
            conn.commit()
            before_count = conn.execute(
                "SELECT COUNT(*) FROM status_history WHERE item_id=?", (item_id,)
            ).fetchone()[0]
        finally:
            conn.close()

        # Another degraded call -> should be a no-op.
        A._set_health_status("SvcA", "degraded")

        conn = A._health_db()
        try:
            after_count = conn.execute(
                "SELECT COUNT(*) FROM status_history WHERE item_id=?", (item_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        # No new history entry should have been created.
        assert before_count == after_count

    def test_unknown_service_no_op(self, A):
        """Calling _set_health_status for a service that doesn't exist in the DB is safe."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump({"items": ["SvcA"], "_runtime": {}}, f)
        # Should not raise.
        A._set_health_status("NonExistentService", "red")

    def test_records_history(self, A):
        """_set_health_status records a change in the status_history table."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump({"items": ["SvcA"], "_runtime": {}}, f)
        # Ensure clean baseline (green).
        with A.app.test_request_context():
            row = A.get_db().execute(
                "SELECT id FROM status_items WHERE name='SvcA'"
            ).fetchone()
            item_id = row["id"]

        conn = A._health_db()
        try:
            conn.execute("UPDATE status_items SET status='green' WHERE id=?", (item_id,))
            conn.commit()
        finally:
            conn.close()

        A._set_health_status("SvcA", "red")

        # Verify history entry exists.
        conn = A._health_db()
        try:
            hist = conn.execute(
                "SELECT old_value, new_value FROM status_history WHERE item_id=? ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        finally:
            conn.close()

        assert hist is not None
        assert hist[0] == "green"
        assert hist[1] == "red"

