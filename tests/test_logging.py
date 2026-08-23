"""Tests for request logging: access.log with client IP + browser info,
app.log security events (login ok/failed/rate-limited), and log rotation.
"""
import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from statuspage import logging_setup  # noqa: E402
import app as app_obj


@pytest.fixture()
def logs(tmp_path, monkeypatch):
    """Point the log directory at tmp and re-init the loggers."""
    monkeypatch.setattr(logging_setup, "_LOG_DIR", tmp_path / "logs")
    # Reset module-level loggers so handlers attach to the new dir
    for name in ("statuspage.access", "statuspage.app"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
    logging_setup.init_logging()
    yield logging_setup._LOG_DIR


def _client(client, path="/", ua="Mozilla/5.0 (X11; Linux) Chrome/120.0", headers=None):
    hdrs = {"User-Agent": ua}
    hdrs.update(headers or {})
    return client.get(path, headers=hdrs)


class TestAccessLog:
    def test_request_logged_with_ip_and_browser(self, client, logs):
        _client(client, ua="Mozilla/5.0 (Windows NT 10.0) Firefox/121.0")
        content = (logs / "access.log").read_text()
        assert "Firefox/Windows" in content          # browser summary
        assert "127.0.0.1" in content                # client IP
        assert "GET /" in content                    # method+path
        assert "200" in content                      # status

    def test_user_agent_recorded(self, client, logs):
        ua = "Mozilla/5.0 (Macintosh) Safari/605.1"
        _client(client, ua=ua)
        content = (logs / "access.log").read_text()
        assert "Safari/macOS" in content
        assert ua[:40] in content                    # raw UA excerpt

    def test_xff_ignored_without_trust_proxy(self, client, logs, monkeypatch):
        """Default: XFF is NOT trusted (spoofable) — remote_addr is logged."""
        monkeypatch.delenv("STATUS_TRUST_PROXY", raising=False)
        _client(client, headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
        content = (logs / "access.log").read_text()
        assert "203.0.113.7" not in content
        assert "127.0.0.1" in content

    def test_x_forwarded_for_honoured_with_trust_proxy(self, client, logs, monkeypatch):
        """STATUS_TRUST_PROXY=1: leftmost XFF entry = original client."""
        monkeypatch.setenv("STATUS_TRUST_PROXY", "1")
        _client(client, headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
        content = (logs / "access.log").read_text()
        assert "203.0.113.7" in content              # leftmost = original client

    def test_missing_ua_placeholder(self, client, logs):
        r = client.get("/", headers={"User-Agent": ""})
        assert r.status_code == 200
        content = (logs / "access.log").read_text()
        # browser summary shows "-" for empty UA
        assert "127.0.0.1 - \"GET /\"" in content

    def test_status_code_and_duration_present(self, client, logs):
        _client(client, path="/definitely-not-a-route")
        content = (logs / "access.log").read_text()
        assert '"GET /definitely-not-a-route" 404' in content
        assert "ms" in content                       # duration recorded


class TestClientIpExtraction:
    def test_direct_remote_addr(self, A):
        with app_obj.app.test_request_context("/", environ_base={"REMOTE_ADDR": "10.1.2.3"}):
            assert logging_setup.client_ip() == "10.1.2.3"

    def test_forwarded_for_ignored_by_default(self, A, monkeypatch):
        """Without STATUS_TRUST_PROXY, XFF must not override remote_addr."""
        monkeypatch.delenv("STATUS_TRUST_PROXY", raising=False)
        with app_obj.app.test_request_context(
            "/", environ_base={"REMOTE_ADDR": "10.0.0.9"},
            headers={"X-Forwarded-For": "198.51.100.5, 10.0.0.1"},
        ):
            assert logging_setup.client_ip() == "10.0.0.9"

    def test_forwarded_for_leftmost_with_trust_proxy(self, A, monkeypatch):
        monkeypatch.setenv("STATUS_TRUST_PROXY", "1")
        with app_obj.app.test_request_context(
            "/", environ_base={"REMOTE_ADDR": "10.0.0.9"},
            headers={"X-Forwarded-For": "198.51.100.5, 10.0.0.1"},
        ):
            assert logging_setup.client_ip() == "198.51.100.5"

    def test_no_remote_addr_placeholder(self, A):
        with app_obj.app.test_request_context("/", environ_base={"REMOTE_ADDR": ""}):
            assert logging_setup.client_ip() == "-"


class TestBrowserSummary:
    @pytest.mark.parametrize("ua,expected", [
        ("Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0", "Chrome/Linux"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64) Edg/120.0", "Edge/Windows"),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1", "Safari/iOS"),
        ("curl/8.0.1", "curl/Unknown-OS"),
        ("Totally-Unknown-UA", "Other/Unknown-OS"),
        ("", "-"),
    ])
    def test_summary_parsing(self, A, ua, expected):
        with app_obj.app.test_request_context("/", headers={"User-Agent": ua}):
            assert logging_setup.browser_summary() == expected


class TestSecurityEventLog:
    def test_failed_login_logged_with_ip_and_ua(self, A, logs):
        c = app_obj.app.test_client()
        c.post("/login", json={"user": "admin", "pass": "wrong-password"},
               headers={"User-Agent": "evil-bot/1.0"})
        content = (logs / "app.log").read_text()
        assert "LOGIN failed" in content
        assert "ip=127.0.0.1" in content
        assert "ua='evil-bot/1.0'" in content

    def test_successful_login_logged(self, A, logs):
        c = app_obj.app.test_client()
        r = c.post("/login", json={"user": "admin", "pass": "testpass"},
                   headers={"User-Agent": "good-client/2.0"})
        assert r.status_code == 200
        content = (logs / "app.log").read_text()
        assert "LOGIN ok" in content
        assert "good-client/2.0" in content


class TestLogRotation:
    def test_rotating_handler_configured(self, logs):
        lg = logging.getLogger("statuspage.access")
        handlers = [h for h in lg.handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert handlers, "no RotatingFileHandler on access logger"
        assert handlers[0].maxBytes == 5 * 1024 * 1024
        assert handlers[0].backupCount == 3

    def test_init_logging_idempotent(self, logs):
        """Re-init must not duplicate handlers (gunicorn worker forks)."""
        before = len(logging.getLogger("statuspage.access").handlers)
        logging_setup.init_logging()
        after = len(logging.getLogger("statuspage.access").handlers)
        assert before == after


class TestNoPrintRegression:
    def test_auth_module_has_no_print_calls(self):
        """auth.py must use logging, not print() — prints vanish into
        gunicorn stderr and lose IP/timestamp context."""
        source = (
            Path(__file__).resolve().parent.parent / "statuspage" / "auth.py"
        ).read_text()
        import re
        calls = re.findall(r"^\s*print\(", source, flags=re.MULTILINE)
        assert not calls, f"print() calls found in auth.py: {len(calls)}"
