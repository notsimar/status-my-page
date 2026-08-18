#!/usr/bin/env python3
"""Tiny status page — Flask + SQLite + YAML config.

Modular entry point that wires together all components.
"""

import os
import secrets
from pathlib import Path

from flask import Flask

from statuspage.config import (
    init_config_paths,
    load_config,
    get_server_host,
    get_server_port,
    get_secret_key_env,
    get_base_dir,
    get_db_path,
    get_config_path,
    get_archives_dir,
)
from statuspage.auth import init_admin_auth
from statuspage.db import init_db
from statuspage.healthcheck import configure_healthcheck_module, start_healthchecks
from statuspage.routes import (
    status_page,
    feed_xml,
    api_rss_status,
    api_rss_toggle,
    api_history,
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
)


# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

# These will be dynamically resolved via __getattr__
# Initial values are set but will be overridden by __getattr__ after init_config_paths()
# We don't set CONFIG_PATH, DB_PATH, ARCHIVES_DIR at module level - they'll come from __getattr__


# ── App factory ────────────────────────────────────────────────────
app = Flask(__name__)

# Initialize config paths (this sets up the config module's internal state)
init_config_paths(BASE_DIR)

# After init_config_paths, the config module's getters return the correct paths
# Module-level paths will be resolved via __getattr__ dynamically

# Load config to get secret key env var name
cfg = load_config()
SECRET_ENV = cfg.get("server", {}).get("secret_key_env", "STATUS_SECRET_KEY")
app.secret_key = os.environ.get(SECRET_ENV) or secrets.token_hex(32)

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


# ── Backwards compatibility for tests ──────────────────────────────
# These attributes are expected by the test suite - expose them BEFORE routes
from statuspage import config as _config
from statuspage import auth as _auth
from statuspage import db as _db
from statuspage import healthcheck as _healthcheck

# Expose config functions for tests
init_config_paths = _config.init_config_paths
get_base_dir = _config.get_base_dir
get_db_path = _config.get_db_path
get_config_path = _config.get_config_path
get_archives_dir = _config.get_archives_dir
get_item_names = _config.get_item_names
get_admin_user = _config.get_admin_user
get_server_host = _config.get_server_host
get_server_port = _config.get_server_port
get_secret_key_env = _config.get_secret_key_env
load_config = _config.load_config
reload_config = _config.reload_config
_load_runtime = _config._load_runtime
_save_runtime = _config._save_runtime
MAX_HISTORY_PER_ITEM = 100

# Also expose the config module itself for tests
config = _config

# Expose auth functions for tests
get_admin_pass_hash = _auth.get_admin_pass_hash

# Rate limit state
_failed_logins = _auth._failed_logins
_mutation_rates = _auth._mutation_rates
_csrf_failures = _auth._csrf_failures

# Session idle expiry (used by tests)
enforce_session_idle_expiry = _auth.enforce_session_idle_expiry
ADMIN_ACTIVE_SINCE_KEY = _auth.ADMIN_ACTIVE_SINCE_KEY

# Healthcheck validation functions (used by tests) - re-export from original healthcheck module
import healthcheck as _hc_module
_safe_url = _hc_module._safe_url
_safe_host = _hc_module._safe_host
_safe_port = _hc_module._safe_port
_parse_healthchecks = _hc_module._parse_healthchecks
_run_ping_check = _hc_module._run_ping_check
_run_tcp_check = _hc_module._run_tcp_check
_run_curl_check = _hc_module._run_curl_check
_run_soap_check = _hc_module._run_soap_check
_run_rss_feed_check = _hc_module._run_rss_feed_check
_set_health_status = _hc_module._set_health_status
_healthcheck_worker = _hc_module._healthcheck_worker

# Module-level attributes for test compatibility (dynamic properties)
# These need to be updated when init_config_paths is called

class _Compat:
    @property
    def cfg(self):
        return _config.load_config()
    
    @property
    def ITEM_NAMES(self):
        return _config.get_item_names()
    
    @property
    def CONFIG_PATH(self):
        return _config.get_config_path()
    
    @property
    def DB_PATH(self):
        return _config.get_db_path()
    
    @property
    def ARCHIVES_DIR(self):
        return _config.get_archives_dir()
    
    @property
    def BASE_DIR(self):
        return _config.get_base_dir()
    
    @property
    def MAX_HISTORY_PER_ITEM(self):
        return 100

_compat = _Compat()

# Expose as module attributes (properties for dynamic values)
cfg = _compat.cfg
ITEM_NAMES = _compat.ITEM_NAMES
# CONFIG_PATH, DB_PATH, ARCHIVES_DIR, BASE_DIR are dynamic - use __getattr__

# Re-export functions
init_db = _db.init_db


# Module-level __getattr__ for dynamic attributes (Python 3.7+)
def __getattr__(name):
    if name == "CONFIG_PATH":
        return _config.get_config_path()
    if name == "DB_PATH":
        return _config.get_db_path()
    if name == "ARCHIVES_DIR":
        return _config.get_archives_dir()
    if name == "BASE_DIR":
        return _config.get_base_dir()
    if name == "MAX_HISTORY_PER_ITEM":
        return 100
    # Config constants (used by tests)
    if name == "_NUM_BACKUPS":
        from constants import NUM_CONFIG_BACKUPS
        return NUM_CONFIG_BACKUPS
    if name == "MAX_CSRF_FAILURES":
        from constants import MAX_CSRF_FAILURES
        return MAX_CSRF_FAILURES
    if name == "MUTATION_MAX":
        from constants import MUTATION_MAX
        return MUTATION_MAX
    if name == "MUTATION_WINDOW":
        from constants import MUTATION_WINDOW
        return MUTATION_WINDOW
    # Healthcheck constants (used by tests)
    if name == "HEALTHCHECK_INTERVAL_DEFAULT":
        return 60
    if name == "HEALTHCHECK_TIMEOUT_DEFAULT":
        return 10
    if name == "HEALTHCHECK_RETRIES_DEFAULT":
        return 2
    if name == "CURL_MAX_REDIRS":
        return 5
    if name == "DEFAULT_SOAP_ENVELOPE":
        return '<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body/></soap:Envelope>'
    if name == "_HEALTH_LOCK":
        import threading
        return threading.Lock()
    if name == "_HEALTHCHECK_THREAD":
        return None
    if name == "_HEALTHCHECK_START_LOCK":
        import threading
        return threading.Lock()
    # Database functions (used by tests)
    if name == "get_db":
        from statuspage.db import get_connection
        return get_connection
    # Service functions (used by tests)
    if name == "toggle_item":
        from statuspage.services import toggle_item
        return toggle_item
    if name == "update_item_name":
        from statuspage.services import rename_item
        return rename_item
    if name == "set_notes":
        from statuspage.services import update_notes
        return update_notes
    if name == "add_item":
        from statuspage.services import add_item
        return add_item
    if name == "delete_item":
        from statuspage.services import delete_item
        return delete_item
    if name == "reorder_items":
        from statuspage.services import reorder_items
        return reorder_items
    # Healthcheck functions (used by tests)
    if name == "run_healthchecks_once":
        from statuspage.healthcheck import run_healthchecks_once
        return run_healthchecks_once
    if name == "_health_db":
        import healthcheck as _hc_module
        return _hc_module._health_db
    if name == "_parse_healthchecks":
        import healthcheck as _hc_module
        return _hc_module._parse_healthchecks
    if name == "_safe_url":
        import healthcheck as _hc_module
        return _hc_module._safe_url
    if name == "_safe_host":
        import healthcheck as _hc_module
        return _hc_module._safe_host
    if name == "_safe_port":
        import healthcheck as _hc_module
        return _hc_module._safe_port
    if name == "_run_ping_check":
        import healthcheck as _hc_module
        return _hc_module._run_ping_check
    if name == "_run_tcp_check":
        import healthcheck as _hc_module
        return _hc_module._run_tcp_check
    if name == "_run_curl_check":
        import healthcheck as _hc_module
        return _hc_module._run_curl_check
    if name == "_run_soap_check":
        import healthcheck as _hc_module
        return _hc_module._run_soap_check
    if name == "_run_rss_feed_check":
        import healthcheck as _hc_module
        return _hc_module._run_rss_feed_check
    if name == "RSS_MAX_ITEMS":
        import healthcheck as _hc_module
        return _hc_module.RSS_MAX_ITEMS
    if name == "_set_health_status":
        import healthcheck as _hc_module
        return _hc_module._set_health_status
    if name == "_healthcheck_worker":
        import healthcheck as _hc_module
        return _hc_module._healthcheck_worker
    if name == "HEALTHCHECK_INTERVAL_DEFAULT":
        import healthcheck as _hc_module
        return _hc_module.HEALTHCHECK_INTERVAL_DEFAULT
    if name == "HEALTHCHECK_TIMEOUT_DEFAULT":
        import healthcheck as _hc_module
        return _hc_module.HEALTHCHECK_TIMEOUT_DEFAULT
    if name == "HEALTHCHECK_RETRIES_DEFAULT":
        import healthcheck as _hc_module
        return _hc_module.HEALTHCHECK_RETRIES_DEFAULT
    if name == "CURL_MAX_REDIRS":
        import healthcheck as _hc_module
        return _hc_module.CURL_MAX_REDIRS
    if name == "DEFAULT_SOAP_ENVELOPE":
        import healthcheck as _hc_module
        return _hc_module.DEFAULT_SOAP_ENVELOPE
    # Archive function (used by tests)
    if name == "_archive_db_snapshot":
        from statuspage.db import archive_db_snapshot
        return archive_db_snapshot
    raise AttributeError(f"module 'app' has no attribute '{name}'")


# Also define __dir__ for tab completion
def __dir__():
    return [
        "app", "cfg", "ITEM_NAMES", "CONFIG_PATH", "DB_PATH", "ARCHIVES_DIR", 
        "BASE_DIR", "MAX_HISTORY_PER_ITEM", "init_config_paths", "get_base_dir",
        "get_db_path", "get_config_path", "get_archives_dir", "get_item_names",
        "load_config", "reload_config", "_load_runtime", "_save_runtime",
        "_failed_logins", "_mutation_rates", "_csrf_failures",
        "_safe_url", "_safe_host", "_safe_port", "_parse_healthchecks",
        "_run_ping_check", "_run_tcp_check", "_run_curl_check", "_run_soap_check",
        "_set_health_status", "_healthcheck_worker",
        "init_db", "config", "init_admin_auth", "configure_healthcheck_module",
        "start_healthchecks", "status_page", "api_history", "api_healthchecks",
        "login", "logout", "auth_check", "api_csrf", "api_toggle", "api_rename",
        "api_notes", "api_add", "api_delete", "api_healthcheck_run", "api_reorder",
        "handle_input_rejected", "security_headers",
    ] + list(globals().keys())


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
app.add_url_rule("/api/history/<int:item_id>", "api_history", api_history)
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


# ── Initialize DB and start healthchecks ───────────────────────────
# Ensure DB tables exist and healthcheck thread is started when running under WSGI (e.g. Gunicorn)
if not get_db_path().exists():
    try:
        init_db()
    except Exception:
        pass

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