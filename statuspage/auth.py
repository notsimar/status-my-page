"""Authentication and authorization for status-my-page.

Handles login, session management, CSRF protection, and rate limiting.
"""

import hashlib
import hmac
import time
from collections import defaultdict
from functools import wraps

from flask import abort, request, session, jsonify
from werkzeug.security import check_password_hash, generate_password_hash

from statuspage.config import get_admin_user as config_get_admin_user
from constants import (
    MAX_LOGIN_ATTEMPTS,
    LOCKOUT_SECONDS,
    MUTATION_MAX,
    MUTATION_WINDOW,
    MAX_CSRF_FAILURES,
    ADMIN_SESSION_IDLE_TIMEOUT,
)
from input_filter import InputRejected, validate_json_data, validate_user_input, validate_password


# ── Admin credentials ───────────────────────────────────────────────

# Admin password hash must be set via environment variable
# Generate with: python3 -c 'from werkzeug.security import generate_password_hash; print(generate_password_hash("your-password"))'
ADMIN_PASS_HASH: str | None = None

def init_admin_auth() -> None:
    """Initialize admin authentication. Call once at startup."""
    global ADMIN_PASS_HASH
    import os
    admin_hash_env = os.environ.get("STATUS_ADMIN_PASS_HASH")
    if not admin_hash_env:
        raise RuntimeError(
            "STATUS_ADMIN_PASS_HASH environment variable must be set. "
            "Generate one with:\n"
            "  python3 -c 'from werkzeug.security import generate_password_hash; print(generate_password_hash(\"your-password\"))'"
        )
    ADMIN_PASS_HASH = admin_hash_env


def get_admin_user() -> str:
    return config_get_admin_user()


def get_admin_pass_hash() -> str:
    if ADMIN_PASS_HASH is None:
        raise RuntimeError("Admin auth not initialized. Call init_admin_auth() first.")
    return ADMIN_PASS_HASH


# ── Rate-limiter state ──────────────────────────────────────────────

_failed_logins: dict[str, list[float]] = defaultdict(list)
_mutation_rates: dict[str, list[float]] = defaultdict(list)


def check_mutation_rate(ip: str) -> bool:
    """Return True if IP is allowed to mutate; False if throttled."""
    now = time.time()
    cutoff = now - MUTATION_WINDOW
    _mutation_rates[ip] = [t for t in _mutation_rates[ip] if t > cutoff]
    if len(_mutation_rates[ip]) >= MUTATION_MAX:
        return False
    _mutation_rates[ip].append(now)
    # Purge stale keys
    for k in list(_mutation_rates):
        if not _mutation_rates[k] or _mutation_rates[k][-1] < cutoff - 60:
            del _mutation_rates[k]
    return True


def record_attempt(ip: str) -> None:
    now = time.time()
    _failed_logins[ip] = [t for t in _failed_logins[ip] if now - t < LOCKOUT_SECONDS]
    _failed_logins[ip].append(now)

    # Purge stale keys (prevent mem leak)
    stale = [k for k, ts in _failed_logins.items()
             if not ts or time.time() - max(ts) >= LOCKOUT_SECONDS * 2]
    for k in stale:
        del _failed_logins[k]


def is_locked(ip: str) -> bool:
    return len(_failed_logins.get(ip, [])) >= MAX_LOGIN_ATTEMPTS


# ── CSRF ────────────────────────────────────────────────────────────

CSRF_SESSION_KEY = "_csrf"

_csrf_failures: dict[str, int] = defaultdict(int)


def get_csrf() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        import secrets
        token = secrets.token_hex(32)
        session[CSRF_SESSION_KEY] = token
    return token


def check_csrf() -> bool:
    ip = request.remote_addr or ""
    header_token = request.headers.get("X-CSRF-Token", "")
    # Only accept CSRF token via header (query params are logged)
    sent = header_token

    expected = session.get(CSRF_SESSION_KEY)
    if not expected or not hmac.compare_digest(sent, expected):
        _csrf_failures[ip] += 1
        if _csrf_failures[ip] >= MAX_CSRF_FAILURES:
            session.clear()
            _csrf_failures.pop(ip, None)
        return False

    # Success — rotate token and clear failure counter
    import secrets
    session[CSRF_SESSION_KEY] = secrets.token_hex(32)
    _csrf_failures.pop(ip, None)
    return True


# ── Auth decorators ─────────────────────────────────────────────────

def require_admin(require_csrf: bool = True, require_rate_limit: bool = True):
    """Decorator for admin-only routes with optional CSRF and rate-limit checks."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr or ""
            if not session.get("admin"):
                abort(403)
            if require_csrf and not check_csrf():
                abort(403)
            if require_rate_limit and not check_mutation_rate(ip):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ── Auth routes ─────────────────────────────────────────────────────

ADMIN_ACTIVE_SINCE_KEY = "***"


def enforce_session_idle_expiry() -> None:
    """Slide/expire the admin session based on inactivity.

    Called once per request (app before_request) BEFORE any route runs.
    If the admin was last active more than ADMIN_SESSION_IDLE_TIMEOUT
    seconds ago, the session is wiped (logout). Every request by an
    authenticated admin resets the timer.
    """
    last = session.get(ADMIN_ACTIVE_SINCE_KEY)
    if session.get("admin") and last is not None:
        if time.time() - last > ADMIN_SESSION_IDLE_TIMEOUT:
            session.clear()
            return
        session[ADMIN_ACTIVE_SINCE_KEY] = time.time()


def login_route():
    ip = request.remote_addr or ""

    # Rate limit: lock out IP after too many failures
    if is_locked(ip):
        return jsonify(ok=False, error="Too many attempts. Wait 30s."), 429

    data = validate_json_data(request.get_json(silent=True))
    user_supplied = validate_user_input(data.get("user", ""), "user")
    pass_supplied = validate_password(data.get("pass", ""), "pass")

    # Timing-safe: hash both username and password-hash-result together to prevent user enumeration.
    # Use fixed-length hex string 'true' (4 chars) for the boolean to avoid length-based leaks.
    pass_ok = check_password_hash(get_admin_pass_hash(), pass_supplied)
    if hmac.compare_digest(
        f"{hashlib.sha256(user_supplied.encode()).hexdigest()}{str(pass_ok).lower()}"[:68],
        (f"{hashlib.sha256(get_admin_user().encode()).hexdigest()}true")[:68],
    ):
        session.clear()  # new clean session on login
        session["admin"] = True
        session.permanent = True
        session[ADMIN_ACTIVE_SINCE_KEY] = time.time()  # start the 5-min idle clock
        response = jsonify(ok=True)
        _failed_logins.pop(ip, None)       # unlock current IP on success
        return response

    record_attempt(ip)
    return jsonify(ok=False, error="Invalid credentials"), 401


def logout_route():
    session.clear()
    return jsonify(ok=True)


def auth_check_route():
    return jsonify(admin=session.get("admin", False))


def csrf_token_route():
    if not session.get("admin"):
        abort(403)
    return jsonify(token=get_csrf())