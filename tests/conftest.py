"""Fixtures for status-my-page tests."""

import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# Set these BEFORE any `import app` (conftest is imported before test modules,
# and app.py skips loading .env when STATUS_ADMIN_PASS_HASH is already set).
if "STATUS_ADMIN_PASS_HASH" not in os.environ:
    from werkzeug.security import generate_password_hash
    os.environ["STATUS_ADMIN_PASS_HASH"] = generate_password_hash("testpass")
os.environ["STATUS_NO_ARCHIVE"] = "1"
# Must be set before ANY test imports app, so the healthcheck worker thread
# isn't started at import time (it holds DB connections that break re-init).
os.environ["STATUS_DISABLE_HEALTHCHECKS"] = "1"

import app as app_obj  # noqa: E402
import statuspage.config as _cfg  # noqa: E402
import constants as _consts  # noqa: E402
import statuspage.auth as _auth  # noqa: E402
import statuspage.db as _dbmod  # noqa: E402


# ── Session-scoped: real patching of app paths + first init_db      ──
@pytest.fixture(scope="session")
def A():
    """Import app, repoint ALL paths to a temp env, run init_db once.
       Yields the app module — shared by every test."""

    # ── point at a fresh temp environment ---------------------------
    _td  = tempfile.mkdtemp(prefix="mc_")
    cfg  = Path(_td) / "config.yaml"
    yaml.dump(
        {
            "items": ["SvcA", "SvcB"],
            "_base": {
                "admin": {"user": "admin"},
                "server": {"host": "0.0.0.0", "port": 8920},
            },
        },
        cfg.open("w"),
        default_flow_style=False,
        sort_keys=False,
    )

    # Reinitialize config paths
    _cfg.init_config_paths(Path(_td))

    # Clear rate limit state
    _auth._failed_logins.clear()
    _auth._mutation_rates.clear()
    _auth._csrf_failures.clear()

    # Reload config to pick up the new config.yaml
    _cfg.reload_config()

    with app_obj.app.test_request_context():           # so g is available (get_db needs it)
        _dbmod.init_db()                               # first run — creates tables + seeds

    # Configure healthcheck module with the temp paths
    # Note: Don't call start_healthchecks() here - tests that need it will start it
    import healthcheck as hc
    hc.configure_healthcheck(_cfg.get_base_dir(), _cfg.get_db_path(), _cfg.get_config_path(), _cfg.load_config, _consts.MAX_HISTORY_PER_ITEM)

    yield app_obj  # app module (cfg path available via _cfg.get_config_path() etc.)


# ── Fake Slack webhook (shared by slack + MC/DC suites) ─────────────

@pytest.fixture(scope="session")
def fake_slack_url():
    """Local fake Slack webhook: records payloads to _FakeSlack.payloads."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _FakeSlack(BaseHTTPRequestHandler):
        payloads = []
        fail_with = None  # (status_code, body) tuple or None

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            _FakeSlack.payloads.append(json.loads(body))
            if _FakeSlack.fail_with:
                status, text = _FakeSlack.fail_with
                self.send_response(status)
                self.end_headers()
                self.wfile.write(text.encode())
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

        def log_message(self, format, *args):
            pass

    globals()["_FakeSlack"] = _FakeSlack
    import sys as _sys
    _sys.modules[__name__]._FakeSlack = _FakeSlack

    server = HTTPServer(("127.0.0.1", 0), _FakeSlack)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/services/fake"
    server.shutdown()


# ── Convenience helpers ────────────────────────────────────────

def db_conn(A):
    """Open a raw sqlite3 connection to the current DB_PATH."""
    c = sqlite3.connect(str(_cfg.get_db_path()))
    c.row_factory = sqlite3.Row
    try:
        return c
    finally:
        pass


@pytest.fixture()
def client(A):
    """Test client (no auth)."""
    yield A.app.test_client()


@pytest.fixture()
def admin(client):
    """Login as admin, yield the authenticated client."""
    r = client.post(
        "/login",
        data=json.dumps({"user": "admin", "pass": "testpass"}),
        content_type="application/json",
    )
    assert r.status_code == 200, f"Login: {r.status_code} {r.data}"
    yield client


@pytest.fixture()
def token(admin):
    """Extract CSRF token rendered in the admin page."""
    html = admin.get("/").data.decode()
    m = re.search(r'<meta name="csrf-token" content="([a-f0-9]+)">', html)
    assert m, "No csrf-token meta tag in page"
    yield m.group(1)


@pytest.fixture()
def id_a(A):
    return db_conn(A).execute(
        "SELECT id FROM status_items WHERE name='SvcA'"
    ).fetchone()["id"]


@pytest.fixture()
def id_b(A):
    return db_conn(A).execute(
        "SELECT id FROM status_items WHERE name='SvcB'"
    ).fetchone()["id"]
