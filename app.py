#!/usr/bin/env python3
"""Tiny status page — Flask + SQLite + YAML config."""

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import shutil          # for config.yaml backup rotation
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

import yaml
from flask import (
    Flask, g, render_template, request, jsonify, session, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from input_filter import (
    InputRejected, validate_name, validate_notes,
    validate_user_input, validate_json_data, sanitize_text,
    validate_int_param,
)

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_PATH = BASE_DIR / "instance" / "status.db"
ARCHIVES_DIR = BASE_DIR / "archives"


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



# ── YAML runtime persistence ────────────────────────────────────────
# Admin actions on the page persist back to config.yaml under a _runtime
# section so they survive restarts.  The _CONFIG_LOCK makes concurrent
# saves safe.

_CONFIG_LOCK = threading.Lock()

_NUM_BACKUPS = 5  # How many old versions of config.yaml to keep


def _rotate_backups():
    """Rotate backup files: current → bak1, bak1→bak2, ..., bakN-1→bakN.
    
    Preserves the last N versions of config.yaml on disk so you can recover
    from bad automation or accidental changes.  All file ops run under the
    _CONFIG_LOCK (held by callers) for thread safety.
    """
    cfg_base = CONFIG_PATH          # e.g. /path/to/config.yaml
    if not cfg_base.exists():
        return
    
    backup_dir = cfg_base.parent
    
    # ── 1. Delete oldest rotation candidate (beyond retention count) ──
    oldest = backup_dir / f"{cfg_base.name}.bak{_NUM_BACKUPS}"
    if oldest.exists():
        oldest.unlink()
    
    # ── 2. Shift existing backups upward: bak4→bak5, bak3→bak4, …, bak1→bak2 ──
    for i in range(_NUM_BACKUPS - 1, 0, -1):
        src = backup_dir / f"{cfg_base.name}.bak{i}"
        dst = backup_dir / f"{cfg_base.name}.bak{i+1}"
        if src.exists():
            src.rename(dst)
    
    # ── 3. Save current config.yaml as bak1 (before the new write overwrites it) ──
    bak1 = backup_dir / f"{cfg_base.name}.bak1"
    shutil.copy2(str(cfg_base), str(bak1))


def _load_runtime():
    """Return {status: {name→state}, notes: {name→text}} from config.yaml."""
    try:
        data = load_config()
        return data.get("_runtime", {}) or {}
    except Exception:
        return {}


def _save_runtime(data):
    """Atomically write runtime overrides into config.yaml._runtime.
    
    Before each write, rotates existing backups (current → bak1 → bak2 → ... → bak5),
    keeping the last 5 versions so you can recover from bad automation or accidental changes.
    """
    with _CONFIG_LOCK:
        # Rotate backups before writing new version
        _rotate_backups()
        
        cfg_data = load_config()
        if not isinstance(cfg_data, dict):
            cfg_data = {"items": list(ITEM_NAMES), "_base": {}}

        # Preserve known top-level keys under _base during a rewrite
        for section in ("admin", "server"):
            if section in cfg_data and f"_base.{section}" not in str(cfg_data.get("_base", {})):
                cfg_data.setdefault("_base", {})[section] = (cfg_data.pop(section, {}))

        cfg_data["_runtime"] = data

        path = CONFIG_PATH  # module-level variable
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".config_", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            yaml.dump(cfg_data, fh, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, path)


# ── App factory ────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get(SECRET_ENV) or secrets.token_hex(32)


# ── Global error handler for input validation failures ────────────
@app.errorhandler(InputRejected)
def handle_input_rejected(err: InputRejected):
    return jsonify(error=err.reason), 400

# ── Rate-limiter state ─────────────────────────────────────────────
MAX_LOGIN_ATTEMPTS = 5          # failures before lockout
LOCKOUT_SECONDS = 30            # how long to block that IP

_failed_logins: dict[str, list[float]] = defaultdict(list)

# Mutation rate-limit: max N mutations per IP within a window
MUTATION_MAX = 60               # requests per window
MUTATION_WINDOW = 60            # seconds
_mutation_rates: dict[str, list[float]] = defaultdict(list)


def _check_mutation_rate(ip: str) -> bool:
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
        # WAL mode for better concurrent read/write performance (2+ workers)
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _archive_db_snapshot():
    """Take a timestamped JSON snapshot of current DB state before init_db() resets it.

    Archives are stored in `archives/YYYYMMDD_HHMMSS.json` and can be restored
    manually or programmatically.  Set env var STATUS_NO_ARCHIVE=1 to skip
    (useful during testing).
    """
    if os.environ.get("STATUS_NO_ARCHIVE"):
        return
    if not DB_PATH.exists():
        return

    try:
        archive_db = sqlite3.connect(str(DB_PATH))
        archive_db.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return

    try:
        rows = list(archive_db.execute(
            "SELECT id, name, status, notes, position FROM status_items ORDER BY position"
        ).fetchall())
    except sqlite3.OperationalError:
        archive_db.close()
        return

    if not rows:
        archive_db.close()
        return

    ARCHIVES_DIR.mkdir(exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    filename = ARCHIVES_DIR / f"{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    snapshot_data = {
        "timestamp": ts,
        "items": [{"id": r["id"], "name": r["name"], "status": r["status"],
                    "notes": r["notes"], "position": r["position"]} for r in rows],
    }

    archive_db.close()

    # Write atomically so partial restarts don't corrupt archives
    fd, tmp_path = tempfile.mkstemp(dir=str(ARCHIVES_DIR), prefix=".archive_", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(snapshot_data, fh, indent=2)
    os.replace(tmp_path, str(filename))

    reds = sum(1 for r in snapshot_data["items"] if r["status"] == "red")
    total = len(snapshot_data["items"])
    print(f"Archived {total} items ({reds} red) -> {filename.name}")


def init_db():
    """Initialize/migrate DB tables and seed items from config.yaml.

    Takes a timestamped JSON snapshot of the live DB state (into archives/)
    before seeding so admin changes survive across restarts. Archive can be
    disabled for testing with STATUS_NO_ARCHIVE=1.
    """
    # ── Pre-reset archival — save current DB state before init_db() wipes it ──
    _archive_db_snapshot()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

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

    # ── Restore runtime overrides from YAML after seeding ──────────
    try:
        rt = _load_runtime()
        # Status overrides: {item_name: "degraded"|"red"}
        for item_name, new_state in rt.get("status", {}).items():
            if item_name not in seed_set or new_state in ("green", ""):
                continue
            row = db.execute(
                "SELECT id FROM status_items WHERE name = ?", [item_name]
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE status_items SET status=? WHERE id=?",
                    (new_state, row["id"]),
                )

        # Notes overrides: {item_name: note_text}
        for item_name, note_text in rt.get("notes", {}).items():
            if item_name not in seed_set or not note_text.strip():
                continue
            row = db.execute(
                "SELECT id FROM status_items WHERE name = ?", [item_name]
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE status_items SET notes=? WHERE id=?",
                    (note_text, row["id"]),
                )

        # Reorder overrides: [name, name, ...]
        reorder_list = rt.get("reorder", None)
        if reorder_list and isinstance(reorder_list, list):
            for i, item_name in enumerate(reorder_list):
                row = db.execute(
                    "SELECT id FROM status_items WHERE name = ?", [item_name]
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE status_items SET position=? WHERE id=?",
                        (i + 1, row["id"]),
                    )

    except Exception:
        pass

    action = f'Rebuilt {len(seed_items)} config items from config.yaml'
    if deleted_count:
        action += f" ({deleted_count} removed)"
    if inserted_count:
        action += f", {inserted_count} added"
    print(action)

    # ── History table — tracks every status/notes change ──────────
    db.execute(
        """CREATE TABLE IF NOT EXISTS status_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER NOT NULL REFERENCES status_items(id),
            event_type TEXT    NOT NULL DEFAULT 'status',
            old_value  TEXT    DEFAULT '',
            new_value  TEXT    DEFAULT '',
            occurred   TEXT    NOT NULL
        )"""
    )

    # Backfill `occurred` column for pre-existing databases
    try:
        db.execute("ALTER TABLE status_history ADD COLUMN occurred TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z'")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # ── Restore history from _runtime.history (survives restarts) ──
    try:
        rt = _load_runtime()
        for item_name, entries in rt.get("history", {}).items():
            row = db.execute(
                "SELECT id FROM status_items WHERE name = ?", [item_name]
            ).fetchone()
            if not row:
                continue
            for entry in entries:
                db.execute(
                    "INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) VALUES (?, ?, ?, ?, ?)",
                    (row["id"], entry.get("event_type", "status"),
                     entry.get("old_value", ""), entry.get("new_value", ""),
                     entry.get("occurred", "1970-01-01T00:00:00Z")),
                )
    except Exception:
        pass

    db.commit()
    db.close()


def get_all_items():
    # Red first, then degraded, then green — each group keeps its config-file position
    return get_db().execute(
        "SELECT * FROM status_items ORDER BY CASE status WHEN 'red' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END, position"
    ).fetchall()


MAX_HISTORY_PER_ITEM = 100      # prune older entries per item to bound table growth
HISTORY_RUNTIME_CAP = 25        # history entries persisted to config.yaml per item (survives restarts)


def _record_history(item_id: int, event_type: str, old_value: str, new_value: str):
    """Insert a history row and prune old entries. Called inside same transaction as mutation."""
    db = get_db()
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    db.execute(
        "INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) VALUES (?, ?, ?, ?, ?)",
        (item_id, event_type, old_value, new_value, ts),
    )
    # Prune oldest entries beyond retention limit for this item
    db.execute(
        "DELETE FROM status_history WHERE id NOT IN ("
        "  SELECT id FROM status_history WHERE item_id = ? ORDER BY id DESC LIMIT ?"
        ")",
        (item_id, MAX_HISTORY_PER_ITEM),
    )

    # ── Persist to YAML _runtime.history so it survives restarts ──
    row_name = db.execute(
        "SELECT name FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    if row_name:
        rt = _load_runtime()
        hist = rt.setdefault("history", {})
        item_hist = hist.setdefault(row_name["name"], [])
        item_hist.append({
            "event_type": event_type,
            "old_value": old_value,
            "new_value": new_value,
            "occurred": ts,
        })
        # Keep only the most recent HISTORY_RUNTIME_CAP entries per item in YAML
        hist[row_name["name"]] = item_hist[-HISTORY_RUNTIME_CAP:]
        _save_runtime(rt)


STATUS_CYCLE = ["green", "degraded", "red"]

def toggle_item(item_id: int) -> str:
    """Cycle: green → degraded → red → green (also persists to yaml)."""
    db = get_db()
    row = db.execute(
        "SELECT id, status FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    current = row["status"]
    next_idx = (STATUS_CYCLE.index(current) + 1) % len(STATUS_CYCLE)
    new_status = STATUS_CYCLE[next_idx]
    db.execute(
        "UPDATE status_items SET status=? WHERE id=?",
        (new_status, item_id),
    )

    # Record history
    _record_history(item_id, "status", current, new_status)

    # Persist config-item status changes to yaml _runtime.status
    row_name = db.execute(
        "SELECT name FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    if row_name:
        rt = _load_runtime()
        items_list = cfg.get("items", [])  # current config items list
        item_name = row_name["name"]
        if item_name in items_list:
            rt_status = rt.setdefault("status", {})
            if new_status != "green":
                rt_status[item_name] = new_status
            else:
                rt_status.pop(item_name, None)
            _save_runtime(rt)

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
    # Get current notes for history tracking
    current_row = db.execute("SELECT id, name, notes FROM status_items WHERE id=?", (item_id,)).fetchone()
    old_notes = ""
    if current_row:
        old_notes = current_row["notes"] or ""

    # Record history if notes actually changed
    if old_notes != notes:
        _record_history(item_id, "notes", old_notes, notes)

    # Persist config-item notes to yaml _runtime.notes (config items only)
    if current_row and notes.strip():
        rt = _load_runtime()
        items_list = cfg.get("items", [])
        item_name = current_row["name"]
        if item_name in items_list:
            rt.setdefault("notes", {})[item_name] = notes
            _save_runtime(rt)

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
    is_admin = session.get("admin", False)
    csrf = _get_csrf() if is_admin else ""
    return render_template(
        "index.html", items=items, session_admin=is_admin, csrf_token=csrf
    )


@app.route("/api/csrf-token")
def api_csrf():
    if not session.get("admin"):
        abort(403)
    return jsonify(token=_get_csrf())


@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr or ""

    # Rate limit: lock out IP after too many failures
    if _is_locked(ip):
        return jsonify(ok=False, error="Too many attempts. Wait 30s."), 429

    data = validate_json_data(request.get_json(silent=True))
    user_supplied = validate_user_input(data.get("user", ""), "user")
    pass_supplied = validate_user_input(data.get("pass", ""), "pass")

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
        _failed_logins.pop(ip, None)       # unlock current IP on success
        return response

    _record_attempt(ip)
    return jsonify(ok=False, error="Invalid credentials"), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    resp = jsonify(ok=True)
    return resp


@app.route("/auth-check")
def auth_check():
    return jsonify(admin=session.get("admin", False))


@app.route("/api/toggle/<int:item_id>", methods=["POST"])
def api_toggle(item_id):
    ip = request.remote_addr or ""
    if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip):
        abort(403)
    status = toggle_item(item_id)
    return jsonify(status=status)


@app.route("/api/rename/<int:item_id>", methods=["POST"])
def api_rename(item_id):
    ip = request.remote_addr or ""
    if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip):
        abort(403)
    data = validate_json_data(request.get_json(silent=True))
    name = validate_name(data.get("name", ""), "name")
    update_item_name(item_id, name)
    return jsonify(ok=True)


@app.route("/api/notes/<int:item_id>", methods=["POST"])
def api_notes(item_id):
    ip = request.remote_addr or ""
    if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip):
        abort(403)
    data = validate_json_data(request.get_json(silent=True))
    notes = validate_notes(data.get("notes", ""), "notes")
    set_notes(item_id, notes)
    return jsonify(ok=True)


@app.route("/api/history/<int:item_id>")
def api_history(item_id):
    """Return history for a service. Public read — anyone can see status timeline."""
    db = get_db()
    # Verify item exists (and get its name)
    row = db.execute(
        "SELECT id, name FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    if not row:
        abort(404)

    entries = db.execute(
        "SELECT event_type, old_value, new_value, occurred "
        "FROM status_history WHERE item_id = ? ORDER BY id DESC",
        (item_id,),
    ).fetchall()

    return jsonify({
        "service": row["name"],
        "entries": [
            {
                "event_type": e["event_type"],
                "old_value": e["old_value"],
                "new_value": e["new_value"],
                "occurred": e["occurred"],
            }
            for e in entries
        ]
    })


@app.route("/api/add", methods=["POST"])
def api_add():
    ip = request.remote_addr or ""
    if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip):
        abort(403)
    data = validate_json_data(request.get_json(silent=True))
    name = validate_name(data.get("name", ""), "name")
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
    ip = request.remote_addr or ""
    if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip):
        abort(403)
    db = get_db()
    row = db.execute("SELECT name FROM status_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify(error="Not found"), 404
    name = row["name"]
    db.execute("DELETE FROM status_history WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM status_items WHERE id = ?", (item_id,))
    # Re-index positions to fill the gap
    remaining = db.execute("SELECT id, position FROM status_items ORDER BY position").fetchall()
    for i, r in enumerate(remaining):
        db.execute("UPDATE status_items SET position = ? WHERE id = ?", (i, r["id"]))
    db.commit()
    return jsonify(ok=True, name=name)


@app.route("/api/reorder", methods=["POST"])
def api_reorder():
    ip = request.remote_addr or ""
    if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip):
        abort(403)
    data = validate_json_data(request.get_json(silent=True))
    raw_order = data.get("order", {})
    if not isinstance(raw_order, dict):
        raise InputRejected("order must be an object", "order")
    order_map = {validate_int_param(k, "key"): validate_int_param(v, "value") for k, v in raw_order.items()}
    reorder_items(order_map)
    return jsonify(ok=True)


def _not_admin() -> bool:
    return not session.get("admin")


# ── Main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(f"Status page running on http://0.0.0.0:{SERVER_PORT}")
    print(f"Admin user: {ADMIN_USER} (hash provided via env)")
    app.run(host=SERVER_HOST, port=SERVER_PORT)
