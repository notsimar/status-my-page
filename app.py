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
import subprocess
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
    validate_user_input, validate_json_data,
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


# ── Healthcheck ────────────────────────────────────────────────────
# Optional per-service healthchecks configured in config.yaml under a
# healthchecks: section.  Each entry is keyed by service name (must match
# an item name) and can include:
#   url          — REQUIRED; the HTTP(S) endpoint to curl
#   interval     — seconds between checks (default 60)
#   timeout      — max seconds for curl to wait (default 10)
#   healthy_codes — list of HTTP codes considered healthy (default [200])
#   retries      — consecutive failures before flipping status (default 2)
#
# Example:
#   healthchecks:
#     Web Server:
#       url: http://localhost:8920/
#       interval: 30
#       timeout: 5
#     API Gateway:
#       url: https://api.example.com/health
#       healthy_codes: [200, 204]

HEALTHCHECK_INTERVAL_DEFAULT = 60
HEALTHCHECK_TIMEOUT_DEFAULT  = 10
HEALTHCHECK_RETRIES_DEFAULT  = 2
# Redirect-following limit for curl (SSRF mitigation)
CURL_MAX_REDIRS              = 5

# Mutex for DB writes from the healthcheck thread (avoids conflicting with Flask request threads)
_HEALTH_LOCK = threading.Lock()


def _health_db():
    """Open a standalone SQLite connection for the healthcheck worker thread.

    Does NOT use Flask ``g`` — safe to call outside any request context.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _safe_url(url: str) -> bool:
    """Allow only http:// and https:// URLs (prevent file://, gopher://, etc.)."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except Exception:
        return False


def _parse_healthchecks() -> dict[str, dict]:
    """Reload healthchecks from config.yaml on every call so edits take effect."""
    try:
        cfg_data = load_config()
    except Exception as e:
        print(f"Healthcheck config parse error: {e}")
        return {}

    hc_raw = cfg_data.get("healthchecks", {}) or {}
    healthchecks: dict[str, dict] = {}

    for name, details in hc_raw.items():
        if not isinstance(details, dict):
            continue

        url = details.get("url")
        if not url or not isinstance(url, str) or not url.strip():
            continue
        # Reject non-HTTP(S) URLs (prevent file://, gopher://, etc.)
        if not _safe_url(url.strip()):
            continue

        interval = details.get("interval", HEALTHCHECK_INTERVAL_DEFAULT)
        timeout_val = details.get("timeout", HEALTHCHECK_TIMEOUT_DEFAULT)
        retries = details.get("retries", HEALTHCHECK_RETRIES_DEFAULT)
        healthy_codes = details.get("healthy_codes", [200])

        # Sanitize numerics (reject non-int/float or negatives)
        try:
            interval = int(interval)
            timeout_val = int(timeout_val)
            retries = int(retries)
        except (TypeError, ValueError):
            continue

        if interval <= 0 or timeout_val <= 0 or retries <= 0:
            continue

        # Normalize healthy_codes to a set of ints
        codes = set()
        for c in healthy_codes:
            try:
                codes.add(int(c))
            except (TypeError, ValueError):
                pass
        if not codes:
            codes = {200}

        healthchecks[name.strip()] = {
            "url": url.strip(),
            "interval": max(interval, 1),
            "timeout": max(timeout_val, 1),
            "retries": max(retries, 1),
            "healthy_codes": codes,
        }

    return healthchecks


def _run_curl_check(url: str, timeout: int) -> int | None:
    """Run curl and return the HTTP status code, or None on failure."""
    try:
        result = subprocess.run(
            ["curl", "-o", "/dev/null", "-s", "-w", "%{http_code}",
             "--proto-default", "http",
             "--proto-redir", "-all,http,https",
             "--max-time", str(timeout),
             "--max-redirs", str(CURL_MAX_REDIRS),
             "-L", "--", url],
            capture_output=True, text=True, timeout=max(timeout + 5, CURL_MAX_REDIRS * 2 + 5),
        )
        code_str = result.stdout.strip()
        if not code_str:
            return None
        code = int(code_str)
        # curl returns 000 on connection errors, 3xx means it hit redirect limit
        if code == 0 or code > 599:
            return None
        return code
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


_HEALTHCHECK_THREAD = None
_HEALTHCHECK_START_LOCK = threading.Lock()


def _healthcheck_worker():
    """Background thread that polls each service on its own interval."""
    # Across multiple WSGI worker processes, ensure only one runs the healthcheck loop
    lock_file_path = BASE_DIR / "instance" / ".healthcheck.lock"
    lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_file = open(lock_file_path, "a+")
        try:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError, ImportError):
            # Another worker process is already running the healthcheck worker
            return
    except Exception:
        pass

    # Track consecutive failures per service (for retry/backoff)
    fail_count: dict[str, int] = {}
    next_fire: dict[str, float] = {}

    while True:
        # ── Reload config every cycle so changes without restart work ──
        healthchecks = _parse_healthchecks()
        now = time.time()

        # Synchronize dynamic additions/removals
        for name in list(next_fire):
            if name not in healthchecks:
                del next_fire[name]
                fail_count.pop(name, None)

        for name in healthchecks:
            if name not in next_fire:
                next_fire[name] = now
                fail_count.setdefault(name, 0)

        if not healthchecks:
            # No healthchecks configured — sleep and retry
            time.sleep(HEALTHCHECK_INTERVAL_DEFAULT)
            continue

        # Only check services whose next-fire has passed
        due = [name for name, nf in next_fire.items() if now >= nf]
        for name in due:
            hc = healthchecks.get(name)
            if not hc:
                # Service was removed from config — skip
                continue

            code = _run_curl_check(hc["url"], hc["timeout"])

            if code is not None and code in hc["healthy_codes"]:
                # Healthy — reset fail counter
                if fail_count.get(name, 0) > 0:
                    print(f"Healthcheck OK [{name}] {code} (recovered)")
                fail_count[name] = 0
                _set_health_status(name, "green")

            else:
                # Unhealthy — increment counter
                fail_count[name] = fail_count.get(name, 0) + 1
                attempts = fail_count[name]
                threshold = hc["retries"]

                if attempts >= threshold:
                    status = "red" if attempts >= threshold * 3 else "degraded"
                    _set_health_status(name, status)
                    print(f"Healthcheck FAIL [{name}] attempt={attempts}/{threshold} "
                          f"code={code} -> {status}")

            # Schedule next check for this service on its own interval
            next_fire[name] = time.time() + hc["interval"]

        # Sleep until the next service is due (small increments for clean shutdown)
        if next_fire:
            min_next = min(next_fire.values())
            sleep_dt = max(0.5, min_next - time.time())
            steps = max(1, int(sleep_dt * 2))
            for _ in range(steps):
                time.sleep(0.5)
        else:
            time.sleep(HEALTHCHECK_INTERVAL_DEFAULT)


def run_healthchecks_once() -> dict[str, dict]:
    """Public entry-point: runs all healthchecks once (no DB mutation)."""
    healthchecks = _parse_healthchecks()
    results: dict[str, dict] = {}

    for name, hc in healthchecks.items():
        code = _run_curl_check(hc["url"], hc["timeout"])
        healthy = (code is not None and code in hc["healthy_codes"])
        results[name] = {"status_code": code, "healthy": healthy}

    return results


def _set_health_status(name: str, desired_status: str):
    """Set a DB item's status to match the healthcheck result (green/degraded/red).

    Uses its own DB connection (_health_db) — safe outside Flask request context.
    Writes are serialized via _HEALTH_LOCK to avoid conflicting with Flask threads.
    """
    with _HEALTH_LOCK:
        conn = _health_db()
        try:
            row = conn.execute(
                "SELECT id, status FROM status_items WHERE name = ?", [name]
            ).fetchone()
            if not row:
                return

            current = row["status"]
            if current == desired_status:
                return  # no-op

            ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            conn.execute(
                "UPDATE status_items SET status = ? WHERE id = ?", [desired_status, row["id"]]
            )

            # Record history (actor omitted — healthcheck entries have no user source)
            try:
                conn.execute(
                    "INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) "
                    "VALUES (?, 'status', ?, ?, ?)",
                    (row["id"], current, desired_status, ts),
                )
                conn.execute(
                    "DELETE FROM status_history WHERE id NOT IN ("
                    "  SELECT id FROM status_history WHERE item_id = ? ORDER BY id DESC LIMIT ?"
                    ")",
                    (row["id"], MAX_HISTORY_PER_ITEM),
                )
            except Exception:
                pass

            conn.commit()
        finally:
            conn.close()


def start_healthchecks():
    """Start the healthcheck background daemon thread if not already running."""
    global _HEALTHCHECK_THREAD
    with _HEALTHCHECK_START_LOCK:
        if _HEALTHCHECK_THREAD is not None and _HEALTHCHECK_THREAD.is_alive():
            return
        t = threading.Thread(target=_healthcheck_worker, daemon=True, name="healthcheck")
        t.start()
        _HEALTHCHECK_THREAD = t


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
        dst = backup_dir / f"{cfg_base.name}.bak{i + 1}"
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

    # Use config-driven item names for seeding, merged with runtime-persisted list.
    rt = _load_runtime()
    runtime_items: list[str] = rt.get("items", [])
    seed_items = list(dict.fromkeys(
        [n.strip() for n in ITEM_NAMES if n.strip()] +
        [n.strip() for n in runtime_items if n.strip()]
    )) or [
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
        "SELECT id, name, status FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    if not row:
        return "green"

    current = row["status"]
    next_idx = (STATUS_CYCLE.index(current) + 1) % len(STATUS_CYCLE)
    new_status = STATUS_CYCLE[next_idx]
    db.execute(
        "UPDATE status_items SET status=? WHERE id=?",
        (new_status, item_id),
    )

    # Record history
    _record_history(item_id, "status", current, new_status)

    # Persist status changes to yaml _runtime.status
    item_name = row["name"]
    rt = _load_runtime()
    rt_status = rt.setdefault("status", {})
    if new_status != "green":
        rt_status[item_name] = new_status
    else:
        rt_status.pop(item_name, None)
    _save_runtime(rt)

    db.commit()
    return new_status


def update_item_name(item_id: int, name: str) -> tuple[bool, str]:
    db = get_db()
    row = db.execute("SELECT name FROM status_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return False, "Not found"
    old_name = row["name"]
    if old_name == name:
        return True, "No change"

    # Check for name conflict
    conflict = db.execute("SELECT id FROM status_items WHERE name=? AND id!=?", (name, item_id)).fetchone()
    if conflict:
        return False, "Item already exists"

    db.execute(
        "UPDATE status_items SET name = ? WHERE id = ?", (name, item_id)
    )

    # Update references in _runtime
    rt = _load_runtime()
    updated = False
    if "items" in rt and old_name in rt["items"]:
        rt["items"] = [name if n == old_name else n for n in rt["items"]]
        updated = True
    if "status" in rt and old_name in rt["status"]:
        rt["status"][name] = rt["status"].pop(old_name)
        updated = True
    if "notes" in rt and old_name in rt["notes"]:
        rt["notes"][name] = rt["notes"].pop(old_name)
        updated = True
    if "history" in rt and old_name in rt["history"]:
        rt["history"][name] = rt["history"].pop(old_name)
        updated = True
    if updated:
        _save_runtime(rt)

    db.commit()
    return True, "OK"


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
    if not current_row:
        return
    old_notes = current_row["notes"] or ""

    # Record history if notes actually changed
    if old_notes != notes:
        _record_history(item_id, "notes", old_notes, notes)

    # Persist notes to yaml _runtime.notes (if non-empty)
    if current_row and notes.strip():
        rt = _load_runtime()
        item_name = current_row["name"]
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
    ok, msg = update_item_name(item_id, name)
    if not ok:
        status_code = 404 if msg == "Not found" else 409
        return jsonify(error=msg), status_code
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
    cursor = db.execute(
        "INSERT INTO status_items (name, status, position) VALUES (?, 'green', ?)",
        (name, max_pos + 1),
    )
    new_id = cursor.lastrowid
    db.commit()
    # Persist item names to _runtime.items so they survive restarts
    all_names = [r["name"] for r in db.execute("SELECT name FROM status_items ORDER BY position").fetchall()]
    rt = _load_runtime()
    rt["items"] = all_names
    _save_runtime(rt)
    return jsonify(item={"id": new_id, "name": name, "status": "green", "notes": "", "position": max_pos + 1})


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

    # Update runtime config to prune deleted item
    rt = _load_runtime()
    if "items" in rt and name in rt["items"]:
        rt["items"] = [n for n in rt["items"] if n != name]
    if "status" in rt:
        rt["status"].pop(name, None)
    if "notes" in rt:
        rt["notes"].pop(name, None)
    if "history" in rt:
        rt["history"].pop(name, None)
    _save_runtime(rt)

    return jsonify(ok=True, name=name)


@app.route("/api/healthchecks")
def api_healthchecks():
    """Return all configured healthchecks. Public read — no auth required."""
    hc = _parse_healthchecks()
    # Convert set -> list for JSON serializability
    out: dict[str, dict] = {}
    for name, details in hc.items():
        d = dict(details)
        d["healthy_codes"] = sorted(d["healthy_codes"])
        out[name] = d
    return jsonify(out)


@app.route("/api/healthcheck/run", methods=["POST"])
def api_healthcheck_run():
    """Trigger a one-shot healthcheck run for all services. Admin only."""
    ip = request.remote_addr or ""
    if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip):
        abort(403)

    results: dict[str, dict] = run_healthchecks_once()
    return jsonify(results)


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


# Ensure DB tables exist and healthcheck thread is started when running under WSGI (e.g. Gunicorn)
if not DB_PATH.exists():
    try:
        init_db()
    except Exception:
        pass

start_healthchecks()


# ── Main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    start_healthchecks()
    print(f"Status page running on http://0.0.0.0:{SERVER_PORT}")
    print(f"Admin user: {ADMIN_USER} (hash provided via env)")
    app.run(host=SERVER_HOST, port=SERVER_PORT)
