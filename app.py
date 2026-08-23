#!/usr/bin/env python3
"""Tiny status page — Flask + SQLite + YAML config.

Modular entry point that wires together all components.
"""

import os
import sys
import secrets  # needed for secret key generation
from pathlib import Path

# Load environment variables from .env files before any other imports.
# This ensures STATUS_ADMIN_PASS_HASH, STATUS_SECRET_KEY, and other
# config vars are available when the app module Initializes.
# Priority: .env.local > .env (local overrides global).
try:
    from dotenv import load_dotenv  # available in the project venv
except ImportError:
    load_dotenv = None  # will be handled below

if load_dotenv is not None:
    # Load .env.local first (if exists) so it can override .env
    # Only load if STATUS_ADMIN_PASS_HASH is not already set (preserve test env)
    if "STATUS_ADMIN_PASS_HASH" not in os.environ:
        local_env = Path(__file__).parent / ".env.local"
        if local_env.exists():
            load_dotenv(dotenv_path=str(local_env), override=True)
        # Then load .env as fallback
        global_env = Path(__file__).parent / ".env"
        if global_env.exists():
            load_dotenv(dotenv_path=str(global_env), override=False)

from flask import Flask

from statuspage.config import (
    init_config_paths,
    load_config,
    get_server_host,
    get_server_port,
)
from statuspage.auth import init_admin_auth, init_rate_limit_db
from statuspage.db import init_db
from statuspage.healthcheck import configure_healthcheck_module, start_healthchecks
from statuspage.routes import (
    status_page,
    feed_xml,
    api_rss_status,
    api_rss_toggle,
    api_slack_status,
    api_slack_update,
    api_settings_status,
    api_status_public,
    api_settings_update,
    api_history,
    api_history_clear,
    api_healthchecks,
    api_healthchecks_create,
    api_healthchecks_update,
    api_healthchecks_delete,
    login,
    logout,
    auth_check,
    api_csrf,
    api_toggle,
    api_rename,
    api_notes,
    api_add,
    api_delete,
    api_healthcheck_run,
    api_reorder,
    api_export_static,
)


# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent


# ── App factory ────────────────────────────────────────────────────
app = Flask(__name__)

# Initialize config paths (this sets up the config module's internal state)
init_config_paths(BASE_DIR)

# Structured request + application logging (access.log / app.log with
# client IP and browser info). Must come after init_config_paths so the
# log directory resolves under the install base dir.
from statuspage import logging_setup
logging_setup.init_logging()
logging_setup.register_request_logging(app)

# After init_config_paths, the config module's getters return the correct paths

# Load config to get secret key env var name
cfg = load_config()
SECRET_ENV = cfg.get("server", {}).get("secret_key_env", "STATUS_SECRET_KEY")


def _resolve_secret_key() -> str:
    """Resolve the Flask secret key.

    With multiple Gunicorn workers, a per-process random key breaks sessions
    (each worker would sign cookies differently). Fall back to a key file in
    the instance directory, created once with owner-only permissions, so all
    workers share the same key across restarts.
    """
    env_key = os.environ.get(SECRET_ENV)
    if env_key:
        return env_key

    from statuspage.config import get_base_dir
    key_file = get_base_dir() / "instance" / ".secret_key"
    try:
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
            if key:
                return key
        # O_EXCL create so concurrent gunicorn workers can't each generate a
        # different key (last-writer-wins would leave one worker signing
        # cookies with an orphaned key). Loser of the race re-reads.
        key = secrets.token_hex(32)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key.encode("utf-8"))
        finally:
            os.close(fd)
        return key
    except FileExistsError:
        try:
            key = key_file.read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            pass
        return secrets.token_hex(32)  # unreadable file: dev-only fallback
    except OSError:
        # Last resort (single-process dev only): ephemeral random key.
        return secrets.token_hex(32)


app.secret_key = _resolve_secret_key()

# Session cookie hardening. HttpOnly is Flask's default; we pin SameSite=Lax
# (CSRF defense-in-depth behind the CSRF token) and allow Secure to be
# enabled per-deployment for HTTPS-fronted installs.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(
        os.environ.get("STATUS_SECURE_COOKIES", "").lower() in ("1", "true", "yes")
    ),
)

# Initialize admin auth (requires STATUS_ADMIN_PASS_HASH env var)
init_admin_auth()


# ── Admin session idle expiry (5 min sliding per login) ────────────
# Before every request: expire the admin session if idle beyond the
# timeout, otherwise slide the timer forward. Runs before route checks.
from statuspage.auth import enforce_session_idle_expiry


@app.before_request
def _enforce_admin_idle_expiry():
    enforce_session_idle_expiry()


# ── Per-request DB connection teardown ─────────────────────────────
# get_connection() returns a per-request singleton stored in g. It must be
# closed at the end of every request so no connection (and its write lock)
# leaks across requests — otherwise a rebuild in a later request gets
# "database is locked". Matches the original close_db appcontext hook.
from flask import g as _g


@app.teardown_appcontext
def _close_db(exc):
    db = _g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


# ── Global error handler for input validation failures ────────────
from input_filter import InputRejected

@app.errorhandler(InputRejected)
def handle_input_rejected(err: InputRejected):
    from flask import jsonify
    return jsonify(error=err.reason), 400


# ── Security headers ───────────────────────────────────────────────
@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self';"
    )
    return response


# ── Routes ─────────────────────────────────────────────────────────
app.add_url_rule("/", "status_page", status_page)
app.add_url_rule("/feed.xml", "feed_xml", feed_xml)
app.add_url_rule("/rss", "feed_xml_alias", feed_xml, methods=["GET"])
app.add_url_rule("/api/rss", "api_rss_status", api_rss_status, methods=["GET"])
app.add_url_rule("/api/rss", "api_rss_toggle", api_rss_toggle, methods=["POST"])
app.add_url_rule("/api/slack", "api_slack_status", api_slack_status, methods=["GET"])
app.add_url_rule("/api/slack", "api_slack_update", api_slack_update, methods=["POST"])
app.add_url_rule("/api/settings", "api_settings_status", api_settings_status, methods=["GET"])
app.add_url_rule("/api/status", "api_status_public", api_status_public, methods=["GET"])
app.add_url_rule("/api/settings", "api_settings_update", api_settings_update, methods=["POST"])
app.add_url_rule("/api/history/<int:item_id>", "api_history", api_history)
app.add_url_rule("/api/history/<int:item_id>/clear", "api_history_clear", api_history_clear, methods=["POST"])
app.add_url_rule("/api/healthchecks", "api_healthchecks", api_healthchecks)
app.add_url_rule("/api/healthchecks", "api_healthchecks_create", api_healthchecks_create, methods=["POST"])
app.add_url_rule("/api/healthchecks/<string:name>", "api_healthchecks_update", api_healthchecks_update, methods=["PUT"])
app.add_url_rule("/api/healthchecks/<string:name>", "api_healthchecks_delete", api_healthchecks_delete, methods=["DELETE"])

# Auth routes
app.add_url_rule("/login", "login", login, methods=["POST"])
app.add_url_rule("/logout", "logout", logout, methods=["POST"])
app.add_url_rule("/auth-check", "auth_check", auth_check)
app.add_url_rule("/api/csrf-token", "api_csrf", api_csrf)

# Admin routes
app.add_url_rule("/api/toggle/<int:item_id>", "api_toggle", api_toggle, methods=["POST"])
app.add_url_rule("/api/rename/<int:item_id>", "api_rename", api_rename, methods=["POST"])
app.add_url_rule("/api/notes/<int:item_id>", "api_notes", api_notes, methods=["POST"])
app.add_url_rule("/api/add", "api_add", api_add, methods=["POST"])
app.add_url_rule("/api/delete/<int:item_id>", "api_delete", api_delete, methods=["POST"])
app.add_url_rule("/api/healthcheck/run", "api_healthcheck_run", api_healthcheck_run, methods=["POST"])
app.add_url_rule("/api/reorder", "api_reorder", api_reorder, methods=["POST"])
app.add_url_rule("/api/export/static", "api_export_static", api_export_static, methods=["GET", "POST"])


# ── Initialize DB and start healthchecks ───────────────────────────
# Ensure DB tables exist and healthcheck thread is started when running under WSGI (e.g. Gunicorn).
# Only a brand-new deploy (DB file absent) builds here. If that build fails we
# FAIL FAST (loud log + exit) rather than serve a page on a broken/empty DB —
# a half-working status page is worse than a clearly-down one.
from statuspage.config import get_db_path

if not get_db_path().exists():
    try:
        init_db()
    except Exception as _init_err:
        import traceback as _tb
        sys.stderr.write(
            "\n[status-my-page] FATAL: could not initialize the status database "
            f"at {get_db_path()} — refusing to start.\n"
            f"  {type(_init_err).__name__}: {_init_err}\n{_tb.format_exc()}\n"
        )
        sys.exit(1)

# Rehydrate rate-limiter state (lockouts, mutation counters, CSRF failures)
# from the shared DB so limits survive reloads / worker restarts.
init_rate_limit_db()

# Only start healthchecks if not disabled via env var (for tests)
if not os.environ.get("STATUS_DISABLE_HEALTHCHECKS"):
    configure_healthcheck_module()
    start_healthchecks()


# ── Main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    configure_healthcheck_module()
    start_healthchecks()
    print(f"Status page running on http://0.0.0.0:{get_server_port()}")
    print(f"Admin user: {cfg.get('admin', {}).get('user', 'admin')} (hash provided via env)")
    app.run(host=get_server_host(), port=get_server_port())
