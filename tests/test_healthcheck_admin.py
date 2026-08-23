#!/usr/bin/env python3
"""Tests for admin healthcheck configuration (CRUD API + config persistence).

Covers:
  _save_healthchecks / _load_healthchecks  — atomic YAML writes, backup
                                             rotation, section preservation
  POST /api/healthchecks                   — create (all 4 types, auto-detect,
                                             validation, duplicate 409, auth)
  PUT /api/healthchecks/<name>             — partial update, type change,
                                             clear semantics, 404
  DELETE /api/healthchecks/<name>          — delete, 404, re-create
  GET /api/healthchecks                    — public read reflects writes
  Item deletion                            — prunes matching healthcheck config
  Worker integration                       — entries created via API are visible
                                             to _parse_healthchecks (hot reload)

All tests reuse the A fixture from conftest.py (temp config + DB environment).
"""

import pytest
import yaml
import statuspage.config as _cfg
import constants as _consts
import statuspage.auth as _auth
import healthcheck as _hc
import app as app_obj


# ── Helpers ────────────────────────────────────────────────────────

def _read_hc_yaml() -> dict:
    """Read the healthchecks section straight from config.yaml on disk."""
    import app as m
    with open(str(_cfg.get_config_path())) as f:
        data = yaml.safe_load(f)
    return (data or {}).get("healthchecks") or {}


def _mutate(client, method: str, url: str, payload: dict | None = None):
    """Mutating request with fresh CSRF token and clean rate-limit window.

    CSRF token rotates on every successful mutation, so fetch a new one per
    call. Rate-limit window is reset first so a large test class never trips
    the 60/min mutation cap.
    """
    import app as m
    _auth._mutation_rates.clear()
    tok = client.get("/api/csrf-token").get_json()["token"]
    headers = {"X-CSRF-Token": tok}
    if payload is None:
        return client.open(url, method=method, headers=headers)
    return client.open(url, method=method, json=payload, headers=headers)


UNIQ = 0  # unique suffix for names (avoid collisions across runs of same session)


def _name(prefix: str) -> str:
    global UNIQ
    UNIQ += 1
    return f"{prefix} {UNIQ}x"


@pytest.fixture()
def clean_hc(A):
    """Reset the healthchecks section to {} before and restore after each test.

    Yields the app module so tests can inspect/clear rate-limit state.
    """
    from statuspage.config import _load_healthchecks, _save_healthchecks
    before = _load_healthchecks()
    _save_healthchecks({})
    yield A
    _save_healthchecks(before)


# ── Config persistence (unit-level) ────────────────────────────────

class TestHealthcheckConfigWrite:
    """_save_healthchecks / _load_healthchecks: atomic write + rotation."""

    def test_save_and_load_roundtrip(self, A, clean_hc):
        from statuspage.config import _load_healthchecks, _save_healthchecks
        _save_healthchecks({"SvcA": {"type": "curl", "url": "http://a/health"}})
        assert _load_healthchecks() == {"SvcA": {"type": "curl", "url": "http://a/health"}}

    def test_save_writes_to_disk(self, A, clean_hc):
        from statuspage.config import _save_healthchecks
        _save_healthchecks({"Db": {"type": "tcp", "host": "127.0.0.1", "port": 5432}})
        disk = _read_hc_yaml()
        assert disk == {"Db": {"type": "tcp", "host": "127.0.0.1", "port": 5432}}

    def test_save_preserves_items_section(self, A, clean_hc):
        from statuspage.config import _save_healthchecks, load_config
        items_before = load_config().get("items")
        _save_healthchecks({"X": {"host": "127.0.0.1"}})
        items_after = load_config().get("items")
        assert items_after == items_before

    def test_save_preserves_runtime_section(self, A, clean_hc):
        from statuspage.config import _save_healthchecks, load_config
        rt_before = load_config().get("_runtime") or {}
        _save_healthchecks({"X": {"host": "127.0.0.1"}})
        rt_after = load_config().get("_runtime") or {}
        assert rt_after == rt_before

    def test_save_rotates_backup(self, A, clean_hc):
        from statuspage.config import _save_healthchecks
        _save_healthchecks({"X": {"url": "http://1/"}})
        bak1 = _cfg.get_config_path().parent / "config.yaml.bak1"
        assert bak1.exists(), "_save_healthchecks should rotate a bak1 backup"

    def test_empty_dict_writes_empty_section(self, A, clean_hc):
        from statuspage.config import _load_healthchecks, _save_healthchecks
        _save_healthchecks({})
        assert _load_healthchecks() == {}

    def test_load_when_no_section(self, A, clean_hc):
        from statuspage.config import _load_healthchecks
        # clean_hc wrote {} — also verify a config with no key at all
        with open(str(_cfg.get_config_path())) as f:
            data = yaml.safe_load(f) or {}
        data.pop("healthchecks", None)
        with open(str(_cfg.get_config_path()), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        assert _load_healthchecks() == {}


# ── Validation helpers (unit-level) ────────────────────────────────

class TestHealthcheckValidationHelpers:
    """Shared validators used by the admin endpoints.

    Errors are plain strings (route layer wraps them in 400 responses), so
    these tests run without an app context.
    """

    def test_clean_healthy_codes_valid(self, admin):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes([200, 204])
        assert err is None and codes == [200, 204]

    def test_clean_healthy_codes_drops_out_of_range(self, admin):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes([80, 200, 600, "x"])
        assert err is None and codes == [200]

    def test_clean_healthy_codes_rejects_non_list(self, admin):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes("200")
        assert codes is None and err == "healthy_codes must be an array"

    def test_clean_healthy_codes_absent(self, admin):
        from statuspage.routes import _clean_healthy_codes
        codes, err = _clean_healthy_codes(None)
        assert codes is None and err is None

    def test_validate_url_ok(self, admin):
        from statuspage.routes import _validate_url
        url, err = _validate_url("https://example.com/health")
        assert err is None and url == "https://example.com/health"

    def test_validate_url_file_scheme(self, admin):
        from statuspage.routes import _validate_url
        url, err = _validate_url("file:///etc/passwd")
        assert url is None and err == "url must be http:// or https:// with a valid hostname"

    def test_validate_url_empty(self, admin):
        from statuspage.routes import _validate_url
        url, err = _validate_url("")
        assert url is None and err == "url is required for curl/soap type"

    def test_validate_host_ok(self, admin):
        from statuspage.routes import _validate_host
        host, err = _validate_host("10.0.0.1")
        assert err is None and host == "10.0.0.1"

    def test_validate_host_option_injection(self, admin):
        from statuspage.routes import _validate_host
        host, err = _validate_host("-c")
        assert host is None and err == "invalid host"

    def test_validate_host_shell_injection(self, admin):
        from statuspage.routes import _validate_host
        host, err = _validate_host("127.0.0.1; id")
        assert host is None and err == "invalid host"

    def test_numeric_bounds_ok(self, admin):
        from statuspage.routes import _validate_numeric_fields
        fields, err = _validate_numeric_fields({"interval": 30, "timeout": 5, "retries": 3})
        assert err is None and fields == {"interval": 30, "timeout": 5, "retries": 3}

    def test_numeric_bounds_out_of_range(self, admin):
        from statuspage.routes import _validate_numeric_fields
        fields, err = _validate_numeric_fields({"interval": 99999})
        assert fields == {} and err == "interval must be between 1 and 3600"

    def test_numeric_bool_rejected(self, admin):
        from statuspage.routes import _validate_numeric_fields
        _, err = _validate_numeric_fields({"timeout": True})
        assert err == "timeout must be an integer"


# ── Create endpoint ────────────────────────────────────────────────

class TestHealthcheckCreate:
    """POST /api/healthchecks — all four types + validation."""

    def test_create_curl(self, admin, clean_hc):
        name = _name("HC Curl")
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "curl", "url": "https://example.com/health",
            "interval": 30, "timeout": 5, "retries": 3, "healthy_codes": [200, 204],
        })
        assert r.status_code == 200, r.data
        body = r.get_json()
        assert body["ok"] is True
        assert _read_hc_yaml()[name]["url"] == "https://example.com/health"
        assert body["config"]["healthy_codes"] == [200, 204]

    def test_create_ping(self, admin, clean_hc):
        name = _name("HC Ping")
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "ping", "host": "192.168.10.1", "interval": 15,
        })
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk["type"] == "ping" and disk["host"] == "192.168.10.1"

    def test_create_tcp(self, admin, clean_hc):
        name = _name("HC TCP")
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "tcp", "host": "127.0.0.1", "port": 5432,
        })
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk == {"type": "tcp", "host": "127.0.0.1", "port": 5432}

    def test_create_curl_with_failure_keyword(self, admin, clean_hc):
        name = _name("HC Curl FK")
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "curl", "url": "http://localhost/health",
            "service": "CustomServiceName",
            "failure_keyword": "Error",
            "degraded_keyword": "Slow",
        })
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk["type"] == "curl"
        assert disk["service"] == "CustomServiceName"
        assert disk["failure_keyword"] == "Error"
        assert disk["degraded_keyword"] == "Slow"

    def test_create_soap_all_fields(self, admin, clean_hc):
        name = _name("HC SOAP")
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "soap", "url": "http://localhost:9000/soap",
            "soap_action": "GetStatus", "body": "<soap:Envelope/>",
            "expected_string": "OK", "healthy_codes": [200],
        })
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk["type"] == "soap"
        assert disk["soap_action"] == "GetStatus"
        assert disk["body"] == "<soap:Envelope/>"
        assert disk["expected_string"] == "OK"

    # ── Auto-detection when type omitted ──────────────────────────

    def test_autodetect_curl_from_url(self, admin, clean_hc):
        name = _name("Auto Curl")
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": name, "url": "https://example.com/"})
        assert r.status_code == 200
        assert _read_hc_yaml()[name]["type"] == "curl"

    def test_autodetect_tcp_from_host_port(self, admin, clean_hc):
        name = _name("Auto TCP")
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": name, "host": "127.0.0.1", "port": 6379})
        assert r.status_code == 200
        assert _read_hc_yaml()[name]["type"] == "tcp"

    def test_autodetect_ping_from_host(self, admin, clean_hc):
        name = _name("Auto Ping")
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": name, "host": "10.0.0.1"})
        assert r.status_code == 200
        assert _read_hc_yaml()[name]["type"] == "ping"

    def test_autodetect_soap_from_action(self, admin, clean_hc):
        name = _name("Auto SOAP")
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": name, "url": "http://localhost/soap", "soap_action": "Ping"})
        assert r.status_code == 200
        assert _read_hc_yaml()[name]["type"] == "soap"

    # ── Rejections ────────────────────────────────────────────────

    def test_no_target_rejected(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks", {"name": _name("Dead")})
        assert r.status_code == 400

    def test_invalid_type_rejected(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("Bad"), "type": "smtp", "url": "http://a/"})
        assert r.status_code == 400
        assert "invalid type" in r.get_json()["error"]

    def test_non_string_type_coerced_and_detached(self, admin, clean_hc):
        """type: 123 is not a string — treated as absent, auto-detect kicks in."""
        name = _name("NumType")
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": name, "type": 123, "url": "http://a/"})
        assert r.status_code == 200
        assert _read_hc_yaml()[name]["type"] == "curl"

    def test_duplicate_name_conflict(self, admin, clean_hc):
        name = _name("Dup")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "ping", "host": "10.0.0.1"}).status_code == 200
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": name, "type": "curl", "url": "http://a/"})
        assert r.status_code == 409

    def test_curl_missing_url(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("NoUrl"), "type": "curl"})
        assert r.status_code == 400

    def test_bad_scheme_url(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("BadUrl"), "type": "curl", "url": "file:///etc/passwd"})
        assert r.status_code == 400

    def test_ping_bad_host(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("BadHost"), "type": "ping", "host": "-c"})
        assert r.status_code == 400

    def test_tcp_missing_port(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("NoPort"), "type": "tcp", "host": "127.0.0.1"})
        assert r.status_code == 400

    def test_tcp_port_out_of_range(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("BadPort"), "type": "tcp", "host": "127.0.0.1", "port": 99999})
        assert r.status_code == 400

    def test_tcp_port_bool_rejected(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("BoolPort"), "type": "tcp", "host": "127.0.0.1", "port": True})
        assert r.status_code == 400

    def test_interval_out_of_range(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("Slow"), "type": "curl", "url": "http://a/", "interval": 999999})
        assert r.status_code == 400

    def test_interval_non_integer(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("Frag"), "type": "curl", "url": "http://a/", "interval": "abc"})
        assert r.status_code == 400

    def test_negative_timeout_rejected(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("Neg"), "type": "curl", "url": "http://a/", "timeout": -1})
        assert r.status_code == 400

    def test_healthy_codes_not_list(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("Codes"), "type": "curl", "url": "http://a/",
                     "healthy_codes": "200"})
        assert r.status_code == 400

    def test_empty_name_rejected(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": "", "type": "curl", "url": "http://a/"})
        assert r.status_code == 400

    def test_malformed_json_body(self, admin, clean_hc):
        A = clean_hc  # clean_hc yields the app module
        _auth._mutation_rates.clear()
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post("/api/healthchecks", data="not json",
                       content_type="application/json",
                       headers={"X-CSRF-Token": tok})
        assert r.status_code == 400

    # ── Auth gates ────────────────────────────────────────────────

    def test_unauthenticated_403(self, client, A, clean_hc):
        _auth._mutation_rates.clear()
        r = client.post("/api/healthchecks",
                        json={"name": "X", "type": "ping", "host": "10.0.0.1"})
        assert r.status_code == 403

    def test_bad_csrf_403(self, admin, A, clean_hc):
        _auth._mutation_rates.clear()
        r = admin.post(
            "/api/healthchecks",
            json={"name": "X", "type": "ping", "host": "10.0.0.1"},
            headers={"X-CSRF-Token": "deadbeef"},
        )
        assert r.status_code == 403


# ── Update endpoint ────────────────────────────────────────────────

class TestHealthcheckUpdate:
    """PUT /api/healthchecks/<name> — partial semantics."""

    def test_update_interval_only(self, admin, clean_hc):
        name = _name("Upd")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "curl", "url": "http://a/",
                        "interval": 60, "healthy_codes": [200, 204]}).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}", {"interval": 15})
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk["interval"] == 15
        assert disk["url"] == "http://a/"            # untouched
        assert disk["healthy_codes"] == [200, 204]   # untouched

    def test_update_nonexistent_404(self, admin, clean_hc):
        r = _mutate(admin, "PUT", "/api/healthchecks/Nope", {"interval": 30})
        assert r.status_code == 404

    def test_change_type_drops_old_fields(self, admin, clean_hc):
        name = _name("Retype")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "curl", "url": "http://a/",
                        "healthy_codes": [204]}).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}",
                    {"type": "ping", "host": "10.1.1.1"})
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk == {"type": "ping", "host": "10.1.1.1"}  # url/codes gone

    def test_clear_healthy_codes(self, admin, clean_hc):
        name = _name("ClearCodes")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "curl", "url": "http://a/",
                        "healthy_codes": [200, 204]}).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}", {"healthy_codes": []})
        assert r.status_code == 200
        assert "healthy_codes" not in _read_hc_yaml()[name]

    def test_update_invalid_url(self, admin, clean_hc):
        name = _name("UpdUrl")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "curl", "url": "http://a/"}).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}",
                    {"url": "gopher://evil.net"})
        assert r.status_code == 400
        # unchanged on failure
        assert _read_hc_yaml()[name]["url"] == "http://a/"

    def test_update_invalid_type(self, admin, clean_hc):
        name = _name("UpdType")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "ping", "host": "10.0.0.1"}).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}", {"type": "icmp"})
        assert r.status_code == 400

    def test_update_numeric_out_of_range(self, admin, clean_hc):
        name = _name("UpdNum")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "ping", "host": "10.0.0.1"}).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}", {"retries": 99})
        assert r.status_code == 400

    def test_update_soap_clear_body(self, admin, clean_hc):
        name = _name("UpdSoap")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "soap", "url": "http://a/",
                        "soap_action": "A", "body": "<x/>"}).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}", {"body": ""})
        assert r.status_code == 200
        assert "body" not in _read_hc_yaml()[name]


# ── Delete endpoint ────────────────────────────────────────────────

class TestHealthcheckDelete:
    """DELETE /api/healthchecks/<name>."""

    def test_delete_existing(self, admin, clean_hc):
        name = _name("Del")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "ping", "host": "10.0.0.1"}).status_code == 200
        r = _mutate(admin, "DELETE", f"/api/healthchecks/{name}")
        assert r.status_code == 200
        assert r.get_json() == {"ok": True}
        assert name not in _read_hc_yaml()

    def test_delete_nonexistent_404(self, admin, clean_hc):
        r = _mutate(admin, "DELETE", "/api/healthchecks/Nope")
        assert r.status_code == 404

    def test_recreate_after_delete(self, admin, clean_hc):
        name = _name("Recreate")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "host": "10.0.0.1"}).status_code == 200
        assert _mutate(admin, "DELETE", f"/api/healthchecks/{name}").status_code == 200
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": name, "url": "http://a/"})
        assert r.status_code == 200
        assert _read_hc_yaml()[name]["type"] == "curl"

    def test_delete_unauthenticated_403(self, client, A, clean_hc):
        _auth._mutation_rates.clear()
        r = client.delete("/api/healthchecks/X")
        assert r.status_code == 403


# ── Cross-cutting behavior ─────────────────────────────────────────

class TestHealthcheckIntegration:
    """API writes ↔ public read, worker parsing, item deletion."""

    def test_public_read_reflects_write_redacted(self, admin, A, clean_hc, monkeypatch):
        monkeypatch.setattr("statuspage.healthcheck._MODULE_CONFIGURED", True)
        """Public readers see the check exist but NOT its probe target."""
        name = _name("Pub")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "tcp", "host": "127.0.0.1",
                        "port": 5432}).status_code == 200
        # Unauthenticated client reads the public endpoint
        public_client = app_obj.app.test_client()
        r = public_client.get("/api/healthchecks")
        assert r.status_code == 200
        body = r.get_json()
        assert name in body
        assert body[name]["type"] == "tcp"
        # Probe target and port are internal detail — redacted publicly
        assert "host" not in body[name]
        assert "port" not in body[name]
        # Admin still sees the full config
        ar = admin.get("/api/healthchecks")
        assert ar.status_code == 200
        abody = ar.get_json()
        assert abody[name]["port"] == 5432
        assert abody[name]["host"] == "127.0.0.1"

    def test_worker_parser_sees_new_entry(self, admin, A, clean_hc):
        """The background worker hot-reloads config; an entry created via API
        must be picked up by _parse_healthchecks() without a restart."""
        import healthcheck as hc
        name = _name("Worker")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "ping", "host": "10.9.9.9",
                        "interval": 5}).status_code == 200
        parsed = hc._parse_healthchecks()
        assert name in parsed, "worker config reload must see API-created entries"
        assert parsed[name]["interval"] == 5

    def test_worker_parser_ignores_garbage_created_manually(self, admin, A, clean_hc):
        """Entries with bad config are skipped by the parser (defensive)."""
        import healthcheck as hc
        with open(str(_cfg.get_config_path())) as f:
            data = yaml.safe_load(f) or {}
        data["healthchecks"] = {"Bad": {"url": "file:///etc/passwd"}}
        with open(str(_cfg.get_config_path()), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        assert "Bad" not in hc._parse_healthchecks()

    def test_item_delete_prunes_healthcheck(self, admin, A, id_a, clean_hc):
        """Deleting a status item also removes its healthcheck config entry."""
        # SvcA's id — name may be unique
        from statuspage.config import _load_healthchecks, _save_healthchecks
        _save_healthchecks({"SvcA": {"type": "tcp", "host": "127.0.0.1", "port": 1234}})
        r = _mutate(admin, "POST", f"/api/delete/{id_a}")
        assert r.status_code == 200
        assert "SvcA" not in _load_healthchecks()

    def test_healthchecks_enabled_helper(self, A):
        """statuspage.config.healthchecks_enabled() reads from config.yaml."""
        from statuspage.config import healthchecks_enabled, _save_settings, _load_settings
        orig = _load_settings()
        try:
            _save_settings({**orig, "healthchecks_enabled": False})
            assert healthchecks_enabled() is False
            _save_settings({**orig, "healthchecks_enabled": True})
            assert healthchecks_enabled() is True
        finally:
            _save_settings(orig)



# ── Backup rotation under repeated admin writes ────────────────────

class TestHealthcheckBackupRotation:
    def test_many_saves_keep_five_backups(self, A, clean_hc):
        from statuspage.config import _save_healthchecks
        for i in range(8):
            _save_healthchecks({"S": {"url": f"http://{i}/"}})
        import glob
        baks = sorted(glob.glob(str(_cfg.get_config_path().parent / "config.yaml.bak*")))
        assert len(baks) == _consts.NUM_CONFIG_BACKUPS, f"expected {_consts.NUM_CONFIG_BACKUPS} backups, got {len(baks)}"


# ── RSS feed healthcheck type ─────────────────────────────────────
# The rss healthcheck fetches a vendor's own status feed (RSS/Atom) and
# maps feed entry text onto the item status: red keywords -> red, degraded
# keywords -> degraded, clean feed -> green, un-fetchable feed -> degraded/
# red via the usual retry ladder. Parsing is stdlib-only.


class TestRssFeedParse:
    """_parse_healthchecks handling of type: rss entries."""

    def _write(self, A, data):
        """Merge healthchecks into the EXISTING config (never clobbers the
        session's items/_runtime — other test files depend on them)."""
        with open(str(_cfg.get_config_path())) as f:
            existing = yaml.safe_load(f) or {}
        for k, v in data.items():
            if k in ("items", "_runtime"):
                continue  # parsing healthchecks does not depend on either
            existing[k] = v
        with open(str(_cfg.get_config_path()), "w") as f:
            yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

    def test_explicit_type(self, A):
        self._write(
            A,
            {
                "items": ["Vendor"],
                "_runtime": {},
                "healthchecks": {
                    "Vendor": {
                        "type": "rss",
                        "url": "http://status.vendor.com/rss",
                        "keywords": {"red": ["outage"], "degraded": ["degraded"]},
                        "interval": 30, "timeout": 5, "retries": 2,
                    }
                },
            },
        )
        hc = _hc._parse_healthchecks()
        assert "Vendor" in hc
        entry = hc["Vendor"]
        assert entry["type"] == "rss"
        assert entry["url"] == "http://status.vendor.com/rss"
        assert entry["keywords"] == {"red": ["outage"], "degraded": ["degraded"]}
        assert entry["interval"] == 30
        assert entry["timeout"] == 5
        assert entry["retries"] == 2

    def test_auto_detect_not_rss_without_type(self, A):
        """A bare url without type: rss stays curl (no false auto-detect)."""
        self._write(
            A,
            {
                "items": ["Svc"],
                "_runtime": {},
                "healthchecks": {"Svc": {"url": "http://x.com/feed"}},
            },
        )
        hc = _hc._parse_healthchecks()
        assert hc["Svc"]["type"] == "curl"

    def test_missing_url_skipped(self, A):
        self._write(
            A,
            {
                "items": ["Svc"],
                "_runtime": {},
                "healthchecks": {
                    "Svc": {"type": "rss", "keywords": {"red": ["outage"]}}
                },
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_bad_scheme_skipped(self, A):
        self._write(
            A,
            {
                "items": ["Svc"],
                "_runtime": {},
                "healthchecks": {"Svc": {"type": "rss", "url": "file:///tmp/feed.xml"}},
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_keywords_case_folded_and_trimmed(self, A):
        self._write(
            A,
            {
                "items": ["Svc"],
                "_runtime": {},
                "healthchecks": {
                    "Svc": {
                        "type": "rss",
                        "url": "http://x.com/",
                        "keywords": {"red": [" Outage ", ""], "degraded": "partial"},
                    }
                },
            },
        )
        hc = _hc._parse_healthchecks()
        assert hc["Svc"]["keywords"]["red"] == ["outage"]
        assert hc["Svc"]["keywords"]["degraded"] == ["partial"]

    def test_non_dict_keywords_become_empty(self, A):
        """Garbage keywords fall back to empty lists (entry still valid)."""
        self._write(
            A,
            {
                "items": ["Svc"],
                "_runtime": {},
                "healthchecks": {
                    "Svc": {"type": "rss", "url": "http://x.com/", "keywords": "nonsense"}
                },
            },
        )
        hc = _hc._parse_healthchecks()
        assert hc["Svc"]["keywords"] == {"red": [], "degraded": []}

    def test_negative_interval_skipped(self, A):
        self._write(
            A,
            {
                "items": ["Svc"],
                "_runtime": {},
                "healthchecks": {"Svc": {"type": "rss", "url": "http://x.com/", "interval": -1}},
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_api_created_entry_visible_to_worker_parser(self, admin, A, clean_hc):
        """Entry created via the admin API is picked up by the worker parser."""
        import healthcheck as hc_mod
        name = _name("RssWorker")
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": name,
            "type": "rss",
            "url": "http://status.vendor.com/rss",
            "interval": 20,
        })
        assert r.status_code == 200, r.data
        parsed = hc_mod._parse_healthchecks()
        assert name in parsed
        assert parsed[name]["type"] == "rss"
        assert parsed[name]["interval"] == 20


class TestRssFeedApi:
    """Admin CRUD for rss healthchecks + one-shot run serialization."""

    def test_create_rss_custom_keywords(self, admin, clean_hc):
        name = _name("RssCustom")
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "rss", "url": "https://status.vendor.com/rss",
            "keywords": {"red": ["Outage", "down"], "degraded": ["minor"]},
        })
        assert r.status_code == 200, r.data
        disk = _read_hc_yaml()[name]
        assert disk["type"] == "rss"
        assert disk["url"] == "https://status.vendor.com/rss"
        assert disk["keywords"] == {"red": ["outage", "down"], "degraded": ["minor"]}

    def test_create_rss_keyword_defaults(self, admin, clean_hc):
        name = _name("RssDefault")
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "rss", "url": "https://status.vendor.com/",
        })
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert "outage" in disk["keywords"]["red"]
        assert "degraded" in disk["keywords"]["degraded"]

    def test_create_rss_missing_url(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks",
                    {"name": _name("RssNoUrl"), "type": "rss"})
        assert r.status_code == 400

    def test_create_rss_bad_scheme(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": _name("RssBadScheme"), "type": "rss", "url": "file:///tmp/f.xml"})
        assert r.status_code == 400

    def test_create_rss_non_dict_keywords_rejected(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": _name("RssBadKw"), "type": "rss",
            "url": "http://a/", "keywords": "not a dict"})
        assert r.status_code == 400
        assert "keywords" in r.get_json()["error"]

    def test_create_rss_non_array_level_rejected(self, admin, clean_hc):
        r = _mutate(admin, "POST", "/api/healthchecks", {
            "name": _name("RssBadLevel"), "type": "rss",
            "url": "http://a/", "keywords": {"red": "outage string ok",
                                              "degraded": {"bad": "shape"}}})
        assert r.status_code == 400
        assert "degraded" in r.get_json()["error"]

    def test_public_read_redacts_rss_target(self, admin, A, clean_hc, monkeypatch):
        monkeypatch.setattr("statuspage.healthcheck._MODULE_CONFIGURED", True)
        name = _name("RssPub")
        assert _mutate(admin, "POST", "/api/healthchecks",
                       {"name": name, "type": "rss",
                        "url": "https://status.vendor.com/rss"}).status_code == 200
        r = app_obj.app.test_client().get("/api/healthchecks")
        assert r.status_code == 200
        body = r.get_json()
        assert body[name]["type"] == "rss"
        # Public view: the feed url and the keyword lists are redacted
        assert "url" not in body[name]
        assert "keywords" not in body[name]
        # Admin view: full config intact (url + default keywords)
        ar = admin.get("/api/healthchecks")
        abody = ar.get_json()
        assert abody[name]["url"] == "https://status.vendor.com/rss"
        assert isinstance(abody[name]["keywords"]["red"], list)
        assert "outage" in abody[name]["keywords"]["red"]

    def test_update_rss_keywords_full_replace(self, admin, clean_hc):
        name = _name("RssUpdate")
        assert _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "rss", "url": "http://a/",
            "keywords": {"red": ["outage"], "degraded": []},
        }).status_code == 200
        assert _mutate(admin, "PUT", f"/api/healthchecks/{name}", {
            "keywords": {"red": ["down only"], "degraded": ["minor"]},
        }).status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk["keywords"] == {"red": ["down only"], "degraded": ["minor"]}

    def test_update_rss_partial_keeps_url(self, admin, clean_hc):
        name = _name("RssPartial")
        assert _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "rss", "url": "http://original/",
        }).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}",
                    {"interval": 120})
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk["url"] == "http://original/"
        assert disk["interval"] == 120
        assert "keywords" in disk  # defaults survive a partial update

    def test_update_type_curl_to_rss_keeps_url_and_defaults(self, admin, clean_hc):
        """A type change is a full-replace; passing url keeps it, and rss
        defaults (keywords) are applied automatically."""
        name = _name("RssMigrate")
        assert _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "curl", "url": "http://feed/",
            "healthy_codes": [200],
        }).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}",
                    {"type": "rss", "url": "http://feed/"})
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert disk["type"] == "rss"
        assert disk["url"] == "http://feed/"
        assert "outage" in disk["keywords"]["red"]
        assert "healthy_codes" not in disk

    def test_update_type_curl_to_rss_without_url_discards_target(self, admin, clean_hc):
        """Full-replace semantics: switching type without a target leaves
        the entry without a url (the parser then skips it — dead config)."""
        name = _name("RssNoTarget")
        assert _mutate(admin, "POST", "/api/healthchecks", {
            "name": name, "type": "curl", "url": "http://feed/",
        }).status_code == 200
        r = _mutate(admin, "PUT", f"/api/healthchecks/{name}", {"type": "rss"})
        assert r.status_code == 200
        disk = _read_hc_yaml()[name]
        assert "url" not in disk
        import healthcheck as hc_mod
        assert name not in hc_mod._parse_healthchecks()

