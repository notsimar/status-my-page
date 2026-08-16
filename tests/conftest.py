"""Fixtures for status-my-page MC/DC tests."""

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
os.environ["STATUS_NO_ARCHIVE"] = "1"
# Must be set before ANY test imports app, so the healthcheck worker thread
# isn't started at import time (it holds DB connections that break re-init).
os.environ["STATUS_DISABLE_HEALTHCHECKS"] = "1"

# ── Session-scoped: real patching of app paths + first init_db      ──
@pytest.fixture(scope="session")
def A():
    """Import app, repoint ALL paths to a temp env, run init_db once.
       Yields the app module — shared by every test."""
    # Ensure project root is on sys.path once (idempotent).
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from werkzeug.security import generate_password_hash
    if "STATUS_ADMIN_PASS_HASH" not in os.environ:
        os.environ["STATUS_ADMIN_PASS_HASH"] = generate_password_hash("testpass")

    # Disable healthchecks for tests to avoid database locking issues
    os.environ["STATUS_DISABLE_HEALTHCHECKS"] = "1"

    import app as m  # noqa: F811

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
    m.init_config_paths(Path(_td))

    # Clear rate limit state - use the module attributes exposed on m
    m._failed_logins.clear()
    m._mutation_rates.clear()
    m._csrf_failures.clear()

    # Reload config to pick up the new config.yaml
    m.reload_config()

    with m.app.test_request_context():           # so g is available (get_db needs it)
        m.init_db()                               # first run — creates tables + seeds

    # Configure healthcheck module with the temp paths
    # Note: Don't call start_healthchecks() here - tests that need it will start it
    import healthcheck as hc
    hc.configure_healthcheck(m.get_base_dir(), m.get_db_path(), m.get_config_path(), m.load_config, m.MAX_HISTORY_PER_ITEM)

    yield m  # app module (cfg path available via m.CONFIG_PATH etc.)


# ── Convenience helpers ────────────────────────────────────────

def db_conn(A):
    """Open a raw sqlite3 connection to the current DB_PATH."""
    c = sqlite3.connect(str(A.DB_PATH))
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