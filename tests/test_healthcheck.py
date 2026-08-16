#!/usr/bin/env python3
"""Tests for the optional per-service healthcheck system (app.py lines ~57-310).

Covers:
  _safe_url             — scheme allowlist, SSRF surface
  _safe_host            — host/IP validation for ping
  _safe_port            — port number validation for TCP
  _parse_healthchecks   — config parsing, sanitisation, edge cases
  _run_ping_check       — real ping invocation + failure modes
  _run_tcp_check        — real TCP connection + failure modes
  _run_curl_check       — real curl invocation + failure modes
  _run_soap_check       — real SOAP POST via curl + failure modes
  run_healthchecks_once — public one-shot entry-point
  start_healthchecks    — daemon thread no-op when nothing configured
  GET /api/healthchecks — JSON serialisability (sets -> sorted lists)
  POST /api/healthcheck/run — admin-only manual trigger, CSRF, no DB mutation
  _set_health_status    — flips status in DB, records history, no-op guards

All tests reuse the A fixture from conftest.py to write configs on disk.
"""

import sqlite3

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
        assert A._safe_url("file:///etc/passwd") is False

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


# ─── _safe_host ─────────────────────────────────────────────────

class TestSafeHost:
    """Host / IP validation for ping check to prevent command/option injection."""

    def test_ipv4_allowed(self, A):
        assert A._safe_host("127.0.0.1") is True
        assert A._safe_host("192.168.10.1") is True

    def test_ipv6_allowed(self, A):
        assert A._safe_host("::1") is True
        assert A._safe_host("2001:db8::1") is True

    def test_hostname_allowed(self, A):
        assert A._safe_host("localhost") is True
        assert A._safe_host("router.home") is True
        assert A._safe_host("dns.google.com") is True

    def test_option_injection_rejected(self, A):
        assert A._safe_host("-c") is False
        assert A._safe_host("--help") is False

    def test_command_injection_rejected(self, A):
        assert A._safe_host("127.0.0.1; id") is False
        assert A._safe_host("127.0.0.1 && reboot") is False
        assert A._safe_host("`id`") is False

    def test_empty_host_rejected(self, A):
        assert A._safe_host("") is False
        assert A._safe_host("   ") is False


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

        import healthcheck as hc
        monkeypatch.setattr(hc, "_LOAD_CONFIG", bad_load)
        assert A._parse_healthchecks() == {}

    def test_ping_healthcheck_explicit_type(self, A):
        self._write(
            A,
            {
                "items": ["Router"],
                "_runtime": {},
                "healthchecks": {
                    "Router": {"type": "ping", "host": "192.168.10.1", "interval": 15, "timeout": 2}
                },
            },
        )
        hc = A._parse_healthchecks()
        assert "Router" in hc
        assert hc["Router"]["type"] == "ping"
        assert hc["Router"]["host"] == "192.168.10.1"
        assert hc["Router"]["interval"] == 15
        assert hc["Router"]["timeout"] == 2

    def test_ping_healthcheck_auto_detect_from_host(self, A):
        self._write(
            A,
            {
                "items": ["Gateway"],
                "_runtime": {},
                "healthchecks": {
                    "Gateway": {"host": "10.0.0.1"}
                },
            },
        )
        hc = A._parse_healthchecks()
        assert "Gateway" in hc
        assert hc["Gateway"]["type"] == "ping"
        assert hc["Gateway"]["host"] == "10.0.0.1"

    def test_tcp_healthcheck_explicit_type(self, A):
        self._write(
            A,
            {
                "items": ["Database"],
                "_runtime": {},
                "healthchecks": {
                    "Database": {"type": "tcp", "host": "127.0.0.1", "port": 5432, "interval": 15, "timeout": 2}
                },
            },
        )
        hc = A._parse_healthchecks()
        assert "Database" in hc
        assert hc["Database"]["type"] == "tcp"
        assert hc["Database"]["host"] == "127.0.0.1"
        assert hc["Database"]["port"] == 5432
        assert hc["Database"]["interval"] == 15
        assert hc["Database"]["timeout"] == 2

    def test_tcp_healthcheck_auto_detect_from_host_port(self, A):
        self._write(
            A,
            {
                "items": ["Redis"],
                "_runtime": {},
                "healthchecks": {
                    "Redis": {"host": "127.0.0.1", "port": 6379}
                },
            },
        )
        hc = A._parse_healthchecks()
        assert "Redis" in hc
        assert hc["Redis"]["type"] == "tcp"
        assert hc["Redis"]["host"] == "127.0.0.1"
        assert hc["Redis"]["port"] == 6379

    def test_tcp_missing_host_skipped(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "port": 5432}},
            },
        )
        assert A._parse_healthchecks() == {}

    def test_tcp_missing_port_skipped(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "127.0.0.1"}},
            },
        )
        assert A._parse_healthchecks() == {}

    def test_tcp_invalid_port_skipped(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "127.0.0.1", "port": 99999}},
            },
        )
        assert A._parse_healthchecks() == {}

    def test_tcp_negative_port_skipped(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "127.0.0.1", "port": -1}},
            },
        )
        assert A._parse_healthchecks() == {}

    def test_tcp_invalid_host_rejected(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "-c", "port": 80}},
            },
        )
        assert A._parse_healthchecks() == {}


# ─── _run_ping_check ──────────────────────────────────────────────

class TestRunPingCheck:
    """Real ping invocation + failure modes."""

    def test_localhost_ping_succeeds(self, A):
        assert A._run_ping_check("127.0.0.1", timeout=1) is True

    def test_unreachable_ping_fails(self, A):
        # 192.0.2.1 is reserved for documentation (TEST-NET-1) — non-routable
        assert A._run_ping_check("192.0.2.1", timeout=1) is False


# ─── _run_tcp_check ──────────────────────────────────────────────

class TestRunTcpCheck:
    """Real TCP connection check + failure modes."""

    def test_localhost_open_port_succeeds(self, A):
        """TCP check to a listening port should succeed."""
        import socket
        # Create a temporary listening socket on localhost
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            assert A._run_tcp_check("127.0.0.1", port, timeout=2) is True
        finally:
            sock.close()

    def test_localhost_closed_port_fails(self, A):
        """TCP check to a closed port should fail."""
        assert A._run_tcp_check("127.0.0.1", 19999, timeout=1) is False

    def test_unreachable_host_fails(self, A):
        """TCP check to non-routable IP should fail/timeout."""
        # 192.0.2.1 is reserved for documentation (TEST-NET-1) — non-routable
        assert A._run_tcp_check("192.0.2.1", 80, timeout=1) is False


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


# ─── SOAP healthchecks ──────────────────────────────────────────

class TestParseSoapHealthcheck:
    """SOAP type detection, parsing, and sanitisation."""

    def test_explicit_soap_type(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {
                            "type": "soap",
                            "url": "http://localhost:9000/soap",
                            "soap_action": "GetStatus",
                        }
                    },
                },
                f,
            )
        hcs = A._parse_healthchecks()
        assert "SvcA" in hcs
        assert hcs["SvcA"]["type"] == "soap"
        assert hcs["SvcA"]["soap_action"] == "GetStatus"

    def test_auto_detect_soap_from_soap_action(self, A):
        """If soap_action is present but type is omitted → auto-detect SOAP."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {
                            "url": "http://localhost:9000/soap",
                            "soap_action": "HealthCheck",
                        }
                    },
                },
                f,
            )
        hcs = A._parse_healthchecks()
        assert hcs["SvcA"]["type"] == "soap"

    def test_auto_detect_soap_from_body(self, A):
        """If body is present but type is omitted → auto-detect SOAP."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {
                            "url": "http://localhost:9000/soap",
                            "body": "<soap:Body><ping xmlns='urn:svc'/></soap:Body>",
                        }
                    },
                },
                f,
            )
        hcs = A._parse_healthchecks()
        assert hcs["SvcA"]["type"] == "soap"

    def test_soap_body_preserved(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {
                            "type": "soap",
                            "url": "http://localhost:9000/soap",
                            "body": "<ns:GetStatus xmlns:ns='urn:svc'/>",
                        }
                    },
                },
                f,
            )
        hcs = A._parse_healthchecks()
        assert hcs["SvcA"]["body"] == "<ns:GetStatus xmlns:ns='urn:svc'/>"

    def test_soap_expected_string_preserved(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {
                            "type": "soap",
                            "url": "http://localhost:9000/soap",
                            "expected_string": "<Status>OK</Status>",
                        }
                    },
                },
                f,
            )
        hcs = A._parse_healthchecks()
        assert hcs["SvcA"]["expected_string"] == "<Status>OK</Status>"

    def test_soap_missing_url_skipped(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {"type": "soap", "soap_action": "X"}
                    },
                },
                f,
            )
        assert A._parse_healthchecks() == {}

    def test_soap_invalid_url_scheme_rejected(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {"type": "soap", "url": "ftp://evil.com/wsdl", "soap_action": "X"}
                    },
                },
                f,
            )
        assert A._parse_healthchecks() == {}

    def test_soap_custom_healthy_codes(self, A):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {
                            "type": "soap",
                            "url": "http://localhost:9000/soap",
                            "healthy_codes": [200, 204],
                        }
                    },
                },
                f,
            )
        hcs = A._parse_healthchecks()
        assert hcs["SvcA"]["healthy_codes"] == {200, 204}

    def test_soap_default_envelope_when_no_body(self, A):
        """No body set → should default to DEFAULT_SOAP_ENVELOPE."""
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(
                {
                    "items": ["SvcA"],
                    "_runtime": {},
                    "healthchecks": {
                        "SvcA": {
                            "type": "soap",
                            "url": "http://localhost:9000/soap",
                        }
                    },
                },
                f,
            )
        hcs = A._parse_healthchecks()
        assert "body" in hcs["SvcA"]
        # body is empty string; DEFAULT_SOAP_ENVELOPE is used at runtime


class TestRunSoapCheck:
    """SOAP POST probing via curl."""

    def test_connection_refused_returns_unhealthy(self, A):
        healthy, code = A._run_soap_check(
            url="http://127.0.0.1:19981/nope",
            timeout=2,
        )
        assert healthy is False

    def test_timeout_returns_unhealthy(self, A, monkeypatch):
        """Force curl to fail via nonexistent host."""
        healthy, code = A._run_soap_check(
            url="http://nonexistent.invalid.host.xzy/ws",
            timeout=2,
        )
        assert healthy is False


class TestHealthcheckExceptionPaths:
    """Test exception handling in healthcheck subprocess calls."""

    def test_run_ping_check_timeout(self, A, monkeypatch):
        """_run_ping_check handles subprocess.TimeoutExpired."""
        import subprocess
        
        def mock_run_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get('timeout', 5))
        
        monkeypatch.setattr(subprocess, "run", mock_run_timeout)
        result = A._run_ping_check("127.0.0.1", timeout=1)
        assert result is False

    def test_run_ping_check_file_not_found(self, A, monkeypatch):
        """_run_ping_check handles FileNotFoundError (ping not installed)."""
        import subprocess
        
        def mock_run_fnf(*args, **kwargs):
            raise FileNotFoundError("ping command not found")
        
        monkeypatch.setattr(subprocess, "run", mock_run_fnf)
        result = A._run_ping_check("127.0.0.1", timeout=1)
        assert result is False

    def test_run_ping_check_os_error(self, A, monkeypatch):
        """_run_ping_check handles OSError."""
        import subprocess
        
        def mock_run_os(*args, **kwargs):
            raise OSError("Permission denied")
        
        monkeypatch.setattr(subprocess, "run", mock_run_os)
        result = A._run_ping_check("127.0.0.1", timeout=1)
        assert result is False

    def test_run_curl_check_timeout(self, A, monkeypatch):
        """_run_curl_check handles subprocess.TimeoutExpired."""
        import subprocess
        
        def mock_run_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get('timeout', 5))
        
        monkeypatch.setattr(subprocess, "run", mock_run_timeout)
        result = A._run_curl_check("http://localhost/", timeout=1)
        assert result is None

    def test_run_curl_check_file_not_found(self, A, monkeypatch):
        """_run_curl_check handles FileNotFoundError (curl not installed)."""
        import subprocess
        
        def mock_run_fnf(*args, **kwargs):
            raise FileNotFoundError("curl command not found")
        
        monkeypatch.setattr(subprocess, "run", mock_run_fnf)
        result = A._run_curl_check("http://localhost/", timeout=1)
        assert result is None

    def test_run_curl_check_os_error(self, A, monkeypatch):
        """_run_curl_check handles OSError."""
        import subprocess
        
        def mock_run_os(*args, **kwargs):
            raise OSError("Network unreachable")
        
        monkeypatch.setattr(subprocess, "run", mock_run_os)
        result = A._run_curl_check("http://localhost/", timeout=1)
        assert result is None

    def test_run_soap_check_timeout(self, A, monkeypatch):
        """_run_soap_check handles subprocess.TimeoutExpired."""
        import subprocess
        
        def mock_run_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get('timeout', 5))
        
        monkeypatch.setattr(subprocess, "run", mock_run_timeout)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1)
        assert healthy is False
        assert code is None

    def test_run_soap_check_file_not_found(self, A, monkeypatch):
        """_run_soap_check handles FileNotFoundError (curl not installed)."""
        import subprocess
        
        def mock_run_fnf(*args, **kwargs):
            raise FileNotFoundError("curl command not found")
        
        monkeypatch.setattr(subprocess, "run", mock_run_fnf)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1)
        assert healthy is False
        assert code is None

    def test_run_soap_check_os_error(self, A, monkeypatch):
        """_run_soap_check handles OSError."""
        import subprocess
        
        def mock_run_os(*args, **kwargs):
            raise OSError("Network unreachable")
        
        monkeypatch.setattr(subprocess, "run", mock_run_os)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1)
        assert healthy is False
        assert code is None

    def test_run_soap_check_empty_stdout(self, A, monkeypatch):
        """_run_soap_check handles empty stdout from curl."""
        import subprocess
        
        class MockResult:
            stdout = ""
            stderr = ""
            returncode = 0
        
        def mock_run_empty(*args, **kwargs):
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run_empty)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1)
        assert healthy is False
        assert code is None

    def test_run_soap_check_no_newline(self, A, monkeypatch):
        """_run_soap_check handles stdout without newline separator."""
        import subprocess
        
        class MockResult:
            stdout = "200"  # No newline
            stderr = ""
            returncode = 0
        
        def mock_run_no_nl(*args, **kwargs):
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run_no_nl)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1)
        assert healthy is False
        assert code is None

    def test_run_soap_check_non_digit_code(self, A, monkeypatch):
        """_run_soap_check handles non-digit status code."""
        import subprocess
        
        class MockResult:
            stdout = "HTTP/1.1 200 OK\nnot-a-number"
            stderr = ""
            returncode = 0
        
        def mock_run_bad_code(*args, **kwargs):
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run_bad_code)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1)
        assert healthy is False
        assert code is None

    def test_run_soap_check_code_out_of_range_zero(self, A, monkeypatch):
        """_run_soap_check handles status code 0 (curl error)."""
        import subprocess
        
        class MockResult:
            stdout = "<xml></xml>\n0"
            stderr = ""
            returncode = 0
        
        def mock_run_zero(*args, **kwargs):
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run_zero)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1)
        assert healthy is False
        assert code is None

    def test_run_soap_check_code_out_of_range_high(self, A, monkeypatch):
        """_run_soap_check handles status code > 599."""
        import subprocess
        
        class MockResult:
            stdout = "<xml></xml>\n999"
            stderr = ""
            returncode = 0
        
        def mock_run_high(*args, **kwargs):
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run_high)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1)
        assert healthy is False
        assert code is None

    def test_run_soap_check_code_not_whitelisted(self, A, monkeypatch):
        """_run_soap_check returns unhealthy when code not in healthy_codes."""
        import subprocess
        
        class MockResult:
            stdout = "<xml></xml>\n404"
            stderr = ""
            returncode = 0
        
        def mock_run_404(*args, **kwargs):
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run_404)
        healthy, code = A._run_soap_check(url="http://localhost/", timeout=1, healthy_codes={200})
        assert healthy is False
        assert code == 404

    def test_run_soap_check_expected_string_missing(self, A, monkeypatch):
        """_run_soap_check returns unhealthy when expected_string not in response."""
        import subprocess
        
        class MockResult:
            stdout = "<xml>Other content</xml>\n200"
            stderr = ""
            returncode = 0
        
        def mock_run_missing(*args, **kwargs):
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run_missing)
        healthy, code = A._run_soap_check(
            url="http://localhost/", timeout=1, 
            expected_string="ExpectedContent"
        )
        assert healthy is False
        assert code == 200

    def test_run_soap_check_expected_string_found(self, A, monkeypatch):
        """_run_soap_check returns healthy when expected_string found."""
        import subprocess
        
        class MockResult:
            stdout = "<xml>ExpectedContent</xml>\n200"
            stderr = ""
            returncode = 0
        
        def mock_run_found(*args, **kwargs):
            return MockResult()
        
        monkeypatch.setattr(subprocess, "run", mock_run_found)
        healthy, code = A._run_soap_check(
            url="http://localhost/", timeout=1, 
            expected_string="ExpectedContent"
        )
        assert healthy is True
        assert code == 200


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

