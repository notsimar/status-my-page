#!/usr/bin/env python3
"""Tiny status page — Flask + SQLite + YAML config."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import yaml
from flask import (
    Flask, g, render_template, request, jsonify, session, abort
)
from werkzeug.security import generate_password_hash, check_password_hash


# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_PATH = BASE_DIR / "instance" / "status.db"


# ── Config ─────────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


cfg = load_config()

ITEM_NAMES: list[str] = cfg.get("items", [])
CFG_ADMIN_USER = cfg.get("admin", {}).get("user", "admin")
# Do not read config plaintext password — use env var hash only.
# If neither STATUS_ADMIN_PASS_HASH nor a fallback exists, refuse to start.
CFG_ADMIN_PASS_FALLBACK_PLAIN = cfg.get("admin", {}).get("password", "")
SERVER_HOST = cfg.get("server", {}).get("host", "0.0.0.0")
SERVER_PORT = cfg.get("server", {}).get("port", 8920)

# Secret key: env var or generate a per-session random key.
SECRET_ENV = cfg.get("server", {}).get("secret_key_env", "STATUS_SECRET_KEY")


# ── App factory ────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get(SECRET_ENV) or secrets.token_hex(32)

# ── Rate-limiter state ─────────────────────────────────────────────
MAX_LOGIN_ATTEMPTS = 5          # failures before lockout
LOCKOUT_SECONDS = 30            # how long to block that IP

_failed_logins: dict[str, list[float]] = defaultdict(list)


def _record_attempt(ip: str):
    now = time.time()
    _failed_logins[ip] = [t for t in _failed_logins[ip] if now - t < LOCKOUT_SECONDS]
    _failed_logins[ip].append(now)

    # Purge stale keys (prevent mem leak)
    stale = [k for k, ts in _failed_logins.items()
             if not ts or time.time() - max(ts) >= LOCKOUT_SECONDS * 2]
    for k in stale:
        del _failed_logins[k]


def _is_locked(ip: str) -> bool:
    return len(_failed_logins.get(ip, [])) >= MAX_LOGIN_ATTEMPTS


# Admin credentials — env var hash takes priority; fall back to config plaintext only if set.
ADMIN_USER = os.environ.get("STATUS_ADMIN_USER", CFG_ADMIN_USER)
admin_hash_env = os.environ.get("STATUS_ADMIN_PASS_HASH")
if admin_hash_env:
    ADMIN_PASS_HASH = admin_hash_env
elif CFG_ADMIN_PASS_FALLBACK_PLAIN and CFG_ADMIN_PASS_FALLBACK_PLAIN != "changeme":
    ADMIN_PASS_HASH = generate_password_hash(CFG_ADMIN_PASS_FALLBACK_PLAIN)
else:
    # No hash configured — refuse to start with a blank password.
    raise RuntimeError(
        "STATUS_ADMIN_PASS_HASH environment variable must be set in production. "
        "Generate one with:\n"
        "  python3 -c 'from werkzeug.security import generate_password_hash; print(generate_password_hash(\"your-password\"))'"
    )


# ── Database helpers ───────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))

    # Use config-driven item names for seeding
    seed_items = [n.strip() for n in ITEM_NAMES if n.strip()] or [
        "Web Server", "Database", "API Gateway", "CDN", "Auth Service",
        "Payment Processing", "Email Service", "Storage", "Cache Layer",
        "Message Queue", "Search Engine", "ML Pipeline", "Monitoring",
        "Backup System", "DNS",
    ]

    # Schema — create table and backfill columns for older databases
    db.execute(
        """CREATE TABLE IF NOT EXISTS status_items (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'green',
            notes  TEXT DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0
        )"""
    )
    try:
        db.execute("ALTER TABLE status_items ADD COLUMN notes TEXT DEFAULT ''")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Sync on every startup — compare current DB rows against config.
    seed_set = set(seed_items)
    existing_rows = {name: rid for name, rid in
                     db.execute("SELECT name, id FROM status_items").fetchall()}

    deleted_count = 0
    inserted_count = 0
    updated_count = 0

    # Delete items no longer in config
    for name in list(existing_rows):
        if name not in seed_set:
            db.execute("DELETE FROM status_items WHERE id = ?", [existing_rows[name]])
            deleted_count += 1

    # Insert new items (not yet in DB)
    existing_after_delete = {row[0] for row in
                             db.execute("SELECT name, id FROM status_items").fetchall()}
    new_items = [n for n in seed_items if n not in existing_after_delete]
    if new_items:
        max_pos = (db.execute("SELECT COALESCE(MAX(position), 0) FROM status_items").fetchone()[0])
        db.executemany(
            "INSERT INTO status_items (name, status, position) VALUES (?, 'green', ?)",
            [(n, max_pos + i + 1) for i, n in enumerate(new_items)]
        )
        inserted_count = len(new_items)

    # Reset all statuses to green, clear notes, and re-index positions from config order
    for i, name in enumerate(seed_items):
        row = db.execute("SELECT id FROM status_items WHERE name = ?", [name]).fetchone()
        if row:
            db.execute(
                "UPDATE status_items SET status='green', notes='', position=? WHERE id=?",
                (i, row[0])
            )

    action = f"Rebuilt {len(seed_items)} items from config.yaml"
    if deleted_count:
        action += f" ({deleted_count} removed)"
    if inserted_count:
        action += f", {inserted_count} added"
    print(action)

    db.commit()
    db.close()


def get_all_items():
    # Red first, then degraded, then green — each group keeps its config-file position
    return get_db().execute(
        "SELECT * FROM status_items ORDER BY CASE status WHEN 'red' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END, position"
    ).fetchall()


STATUS_CYCLE = ["green", "degraded", "red"]

def toggle_item(item_id: int) -> str:
    """Cycle: green → degraded → red → green"""
    db = get_db()
    row = db.execute(
        "SELECT status FROM status_items WHERE id = ?", (item_id,)
    ).fetchone()
    current = row["status"]
    next_idx = (STATUS_CYCLE.index(current) + 1) % len(STATUS_CYCLE)
    new_status = STATUS_CYCLE[next_idx]
    db.execute(
        "UPDATE status_items SET status = ? WHERE id = ?",
        (new_status, item_id),
    )
    db.commit()
    return new_status


def update_item_name(item_id: int, name: str):
    db = get_db()
    db.execute(
        "UPDATE status_items SET name = ? WHERE id = ?", (name, item_id)
    )
    db.commit()


def reorder_items(order_map: dict[int, int]):
    db = get_db()
    for item_id, order in order_map.items():
        db.execute(
            "UPDATE status_items SET position = ? WHERE id = ?",
            (order, item_id),
        )
    db.commit()


def set_notes(item_id: int, notes: str):
    db = get_db()
    db.execute(
        "UPDATE status_items SET notes = ? WHERE id = ?",
        (notes, item_id),
    )
    db.commit()


# ── CSRF ───────────────────────────────────────────────────────────
CSRF_SESSION_KEY = "_csrf"
MAX_CSRF_FAILURES = 3      # bad tokens before session wipe

_csrf_failures: dict[str, int] = defaultdict(int)


def _get_csrf() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_hex(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _check_csrf() -> bool:
    ip = request.remote_addr or ""
    header_token = request.headers.get("X-CSRF-Token", "")
    query_token = request.args.get(CSRF_SESSION_KEY, "")
    sent = header_token or query_token

    expected = session.get(CSRF_SESSION_KEY)
    if not expected or not hmac.compare_digest(sent, expected):
        _csrf_failures[ip] += 1
        if _csrf_failures[ip] >= MAX_CSRF_FAILURES:
            session.clear()
            _csrf_failures.pop(ip, None)
        return False

    # Success — rotate token and clear failure counter
    session[CSRF_SESSION_KEY] = secrets.token_hex(32)
    _csrf_failures.pop(ip, None)
    return True


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
@app.route("/")
def status_page():
    items = get_all_items()
    is_admin = session.get("admin", False) or request.cookies.get("_admin") == "1"
    csrf = _get_csrf() if is_admin else ""
    return render_template(
        "index.html", items=items, session_admin=is_admin, csrf_token=csrf
    )


@app.route("/api/csrf-token")
def api_csrf():
    is_admin = session.get("admin") or request.cookies.get("_admin") == "1"
    if not is_admin:
        abort(403)
    return jsonify(token=_get_csrf())


@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr or ""

    # Rate limit: lock out IP after too many failures
    if _is_locked(ip):
        return jsonify(ok=False, error="Too many attempts. Wait 30s."), 429

    data = request.get_json(silent=True) or {}
    user_supplied = data.get("user", "")
    pass_supplied = data.get("pass", "")

    # Timing-safe: hash both username and password-hash-result together to prevent user enumeration.
    # Use fixed-length hex string 'true' (4 chars) for the boolean to avoid length-based leaks.
    pass_ok = check_password_hash(ADMIN_PASS_HASH, pass_supplied)
    if hmac.compare_digest(
        f"{hashlib.sha256(user_supplied.encode()).hexdigest()}{str(pass_ok).lower()}"[:68],
        (f"{hashlib.sha256(ADMIN_USER.encode()).hexdigest()}true")[:68],
    ):
        session.clear()  # new clean session on login
        session["admin"] = True
        session.permanent = True
        response = jsonify(ok=True)
        is_https = request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure
        response.set_cookie(
            "_admin", "1",
            httponly=True,
            secure=is_https,          # Secure when behind HTTPS proxy
            samesite="Strict",
            max_age=86400,
            path="/",
        )
        _failed_logins.clear()       # unlock on success
        return response

    _record_attempt(ip)
    return jsonify(ok=False, error="Invalid credentials"), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    resp = jsonify(ok=True)
    resp.delete_cookie("_admin", path="/")
    return resp


@app.route("/auth-check")
def auth_check():
    is_admin = session.get("admin", False) or request.cookies.get("_admin") == "1"
    if request.cookies.get("_admin") == "1" and not session.get("admin"):
        # Re-sync cookie into session for convenience
        session["admin"] = True
        is_admin = True
    return jsonify(admin=is_admin)


@app.route("/api/toggle/<int:item_id>", methods=["POST"])
def api_toggle(item_id):
    if _not_admin() or not _check_csrf():
        abort(403)
    status = toggle_item(item_id)
    return jsonify(status=status)


@app.route("/api/rename/<int:item_id>", methods=["POST"])
def api_rename(item_id):
    if _not_admin() or not _check_csrf():
        abort(403)
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    update_item_name(item_id, name)
    return jsonify(ok=True)


@app.route("/api/notes/<int:item_id>", methods=["POST"])
def api_notes(item_id):
    if _not_admin() or not _check_csrf():
        abort(403)
    data = request.get_json(silent=True) or {}
    notes = data.get("notes", "").strip()
    set_notes(item_id, notes)
    return jsonify(ok=True)


@app.route("/api/add", methods=["POST"])
def api_add():
    if _not_admin() or not _check_csrf():
        abort(403)
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    db = get_db()
    row = db.execute("SELECT id FROM status_items WHERE name = ?", [name]).fetchone()
    if row:
        return jsonify(error="Item already exists"), 409
    max_pos = db.execute("SELECT COALESCE(MAX(position), 0) FROM status_items").fetchone()[0]
    db.execute(
        "INSERT INTO status_items (name, status, position) VALUES (?, 'green', ?)",
        (name, max_pos + 1),
    )
    db.commit()
    new_row = db.execute("SELECT * FROM status_items WHERE name = ?", [name]).fetchone()
    return jsonify(item={"id": new_row["id"], "name": new_row["name"], "status": "green", "notes": "", "position": max_pos + 1})


@app.route("/api/delete/<int:item_id>", methods=["POST"])
def api_delete(item_id):
    if _not_admin() or not _check_csrf():
        abort(403)
    db = get_db()
    row = db.execute("SELECT name FROM status_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify(error="Not found"), 404
    name = row["name"]
    db.execute("DELETE FROM status_items WHERE id = ?", (item_id,))
    # Re-index positions to fill the gap
    remaining = db.execute("SELECT id, position FROM status_items ORDER BY position").fetchall()
    for i, r in enumerate(remaining):
        db.execute("UPDATE status_items SET position = ? WHERE id = ?", (i, r["id"]))
    db.commit()
    return jsonify(ok=True, name=name)


@app.route("/api/reorder", methods=["POST"])
def api_reorder():
    if _not_admin() or not _check_csrf():
        abort(403)
    data = request.get_json(silent=True) or {}
    raw_order = data.get("order", {})
    order_map = {int(k): int(v) for k, v in raw_order.items()}
    reorder_items(order_map)
    return jsonify(ok=True)


def _not_admin() -> bool:
    return not (session.get("admin") or request.cookies.get("_admin") == "1")


# ── Main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(f"Status page running on http://0.0.0.0:{SERVER_PORT}")
    print(f"Admin user: {ADMIN_USER} (hash provided via env)")
    app.run(host=SERVER_HOST, port=SERVER_PORT)
