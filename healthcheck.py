#!/usr/bin/env python3
"""Optional per-service healthcheck system for status-my-page.

Supports automated background and on-demand health checking via:
  - HTTP/HTTPS curl probing (type: curl)
  - ICMP ping reachability probing (type: ping)
"""

import datetime as dt
import ipaddress
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

# Default healthcheck settings
HEALTHCHECK_INTERVAL_DEFAULT = 60
HEALTHCHECK_TIMEOUT_DEFAULT = 10
HEALTHCHECK_RETRIES_DEFAULT = 2
CURL_MAX_REDIRS = 5

# Mutex for DB writes from the healthcheck thread
_HEALTH_LOCK = threading.Lock()
_HEALTHCHECK_THREAD = None
_HEALTHCHECK_START_LOCK = threading.Lock()

# ── Configuration (set by app.configure_healthcheck()) ─────────────
_BASE_DIR: Path | None = None
_DB_PATH: Path | None = None
_CONFIG_PATH: Path | None = None
_LOAD_CONFIG = None
_MAX_HISTORY_PER_ITEM = 100


def configure_healthcheck(
    base_dir: Path,
    db_path: Path,
    config_path: Path,
    load_config_fn,
    max_history_per_item: int = 100,
) -> None:
    """Initialize healthcheck module with paths from app. Call once at startup."""
    global _BASE_DIR, _DB_PATH, _CONFIG_PATH, _LOAD_CONFIG, _MAX_HISTORY_PER_ITEM
    _BASE_DIR = base_dir
    _DB_PATH = db_path
    _CONFIG_PATH = config_path
    _LOAD_CONFIG = load_config_fn
    _MAX_HISTORY_PER_ITEM = max_history_per_item


def _health_db():
    """Open a standalone SQLite connection for the healthcheck worker thread.

    Does NOT use Flask ``g`` — safe to call outside any request context.
    """
    if _DB_PATH is None:
        raise RuntimeError("Healthcheck not configured. Call configure_healthcheck() first.")
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_max_history_per_item() -> int:
    return _MAX_HISTORY_PER_ITEM


def _safe_host(target: str) -> bool:
    """Allow valid IPv4/IPv6 addresses and hostnames for ping checks. Reject options/shell chars."""
    if not target or not isinstance(target, str):
        return False
    target = target.strip()
    if not target or target.startswith("-"):
        return False
    try:
        ipaddress.ip_address(target)
        return True
    except ValueError:
        pass
    if len(target) > 253:
        return False
    return bool(re.match(
        r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$',
        target
    ))


def _safe_url(url: str) -> bool:
    """Allow only http:// and https:// URLs (prevent file://, gopher://, etc.)."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except Exception:
        return False


def _safe_port(port: int) -> bool:
    """Validate port number is in valid range."""
    return isinstance(port, int) and 1 <= port <= 65535


DEFAULT_SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body/></soap:Envelope>'
)


def _parse_healthchecks() -> dict[str, dict]:
    """Reload healthchecks from config.yaml on every call so edits take effect."""
    if _LOAD_CONFIG is None or _CONFIG_PATH is None:
        raise RuntimeError("Healthcheck not configured. Call configure_healthcheck() first.")
    try:
        cfg_data = _LOAD_CONFIG()
    except Exception as e:
        print(f"Healthcheck config parse error: {e}")
        return {}

    if not isinstance(cfg_data, dict):
        return {}

    hc_raw = cfg_data.get("healthchecks", {}) or {}
    healthchecks: dict[str, dict] = {}

    for name, details in hc_raw.items():
        if not isinstance(details, dict):
            continue

        check_type = str(details.get("type", "")).strip().lower()
        url = details.get("url")
        host = details.get("host")
        soap_action = details.get("soap_action", details.get("action", ""))
        soap_body = details.get("body", details.get("envelope", ""))
        expected_string = details.get("expected_string", details.get("expected", ""))

        # Auto-detect check_type if not explicitly set
        if not check_type:
            if soap_action or soap_body:
                check_type = "soap"
            elif host and details.get("port") is not None:
                check_type = "tcp"
            elif host:
                check_type = "ping"
            elif url:
                check_type = "curl"
            else:
                continue

        interval = details.get("interval", HEALTHCHECK_INTERVAL_DEFAULT)
        timeout_val = details.get("timeout", HEALTHCHECK_TIMEOUT_DEFAULT)
        retries = details.get("retries", HEALTHCHECK_RETRIES_DEFAULT)

        # Sanitize numerics (reject non-int/float or negatives)
        try:
            interval = int(interval)
            timeout_val = int(timeout_val)
            retries = int(retries)
        except (TypeError, ValueError):
            continue

        if interval <= 0 or timeout_val <= 0 or retries <= 0:
            continue

        if check_type in ("ping", "icmp"):
            target_host = host or url
            if not target_host or not isinstance(target_host, str):
                continue
            target_host = target_host.strip()
            if not _safe_host(target_host):
                continue
            healthchecks[name.strip()] = {
                "type": "ping",
                "host": target_host,
                "interval": max(interval, 1),
                "timeout": max(timeout_val, 1),
                "retries": max(retries, 1),
            }
        elif check_type == "tcp":
            target_host = host or url
            target_port = details.get("port")
            if not target_host or not isinstance(target_host, str):
                continue
            target_host = target_host.strip()
            if not _safe_host(target_host):
                continue
            if target_port is None or not _safe_port(int(target_port)):
                continue
            healthchecks[name.strip()] = {
                "type": "tcp",
                "host": target_host,
                "port": int(target_port),
                "interval": max(interval, 1),
                "timeout": max(timeout_val, 1),
                "retries": max(retries, 1),
            }
        elif check_type in ("curl", "soap"):
            if not url or not isinstance(url, str) or not url.strip():
                continue
            if not _safe_url(url.strip()):
                continue

            healthy_codes = details.get("healthy_codes", [200])
            codes = set()
            for c in healthy_codes:
                try:
                    codes.add(int(c))
                except (TypeError, ValueError):
                    pass
            if not codes:
                codes = {200}

            if check_type == "soap":
                healthchecks[name.strip()] = {
                    "type": "soap",
                    "url": url.strip(),
                    "soap_action": str(soap_action).strip() if soap_action else "",
                    "body": str(soap_body) if soap_body else "",
                    "expected_string": str(expected_string).strip() if expected_string else "",
                    "interval": max(interval, 1),
                    "timeout": max(timeout_val, 1),
                    "retries": max(retries, 1),
                    "healthy_codes": codes,
                }
            else:
                healthchecks[name.strip()] = {
                    "type": "curl",
                    "url": url.strip(),
                    "interval": max(interval, 1),
                    "timeout": max(timeout_val, 1),
                    "retries": max(retries, 1),
                    "healthy_codes": codes,
                }

    return healthchecks


def _run_ping_check(host: str, timeout: int) -> bool:
    """Run ping and return True if host responds, False on failure/timeout."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), "--", host],
            capture_output=True,
            text=True,
            timeout=max(timeout + 2, 5),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _run_tcp_check(host: str, port: int, timeout: int) -> bool:
    """Run TCP connection check and return True if port is open, False on failure/timeout."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def _run_soap_check(
    url: str,
    timeout: int,
    soap_action: str = "",
    body: str = "",
    healthy_codes: set[int] | None = None,
    expected_string: str = "",
) -> tuple[bool, int | None]:
    """Run a SOAP POST request via curl and return (is_healthy, status_code)."""
    if healthy_codes is None:
        healthy_codes = {200}

    payload = body.strip() if (body and isinstance(body, str) and body.strip()) else DEFAULT_SOAP_ENVELOPE

    cmd = [
        "curl", "-s", "-w", "\n%{http_code}",
        "-X", "POST",
        "-H", "Content-Type: text/xml; charset=utf-8",
    ]
    if soap_action and isinstance(soap_action, str) and soap_action.strip():
        clean_action = re.sub(r'[\r\n]', '', soap_action.strip())
        cmd.extend(["-H", f"SOAPAction: {clean_action}"])

    cmd.extend([
        "-d", "@-",
        "--proto-default", "http",
        "--proto-redir", "-all,http,https",
        "--max-time", str(timeout),
        "--max-redirs", str(CURL_MAX_REDIRS),
        "-L", "--", url
    ])

    try:
        result = subprocess.run(
            cmd,
            input=payload,
            capture_output=True,
            text=True,
            timeout=max(timeout + 5, CURL_MAX_REDIRS * 2 + 5),
        )
        stdout = result.stdout
        if not stdout or "\n" not in stdout:
            return False, None

        parts = stdout.rsplit("\n", 1)
        resp_body = parts[0]
        code_str = parts[1].strip()

        if not code_str.isdigit():
            return False, None

        code = int(code_str)
        if code == 0 or code > 599:
            return False, None

        if code not in healthy_codes:
            return False, code

        if expected_string and isinstance(expected_string, str) and expected_string.strip():
            if expected_string.strip() not in resp_body:
                return False, code

        return True, code
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, None


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
        if code == 0 or code > 599:
            return None
        return code
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


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
                    (row["id"], _get_max_history_per_item()),
                )
            except Exception:
                pass

            conn.commit()
        finally:
            conn.close()


def _healthcheck_worker():
    """Background thread that polls each service on its own interval.

    A file-based advisory lock (instance/.healthcheck.lock, fcntl) ensures only
    one worker process runs the loop at a time. The worker holds NO persistent
    DB connection: it opens a WAL connection only briefly when flipping a
    status, so it never contends with admin writes or DB rebuilds.
    """
    _shutdown = threading.Event()

    if _DB_PATH is None:
        raise RuntimeError("Healthcheck not configured. Call configure_healthcheck() first.")

    # Acquire file lock - only one worker process runs the loop at a time
    lock_file_path = _BASE_DIR / "instance" / ".healthcheck.lock"
    lock_file_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = None
    try:
        lock_file = open(lock_file_path, "a+")
        try:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError, ImportError):
            # Another worker process is already running the healthcheck worker
            lock_file.close()
            return
    except Exception:
        lock_file = None  # couldn't even open the lock file; proceed unlocked

    # Track consecutive failures per service (for retry/backoff)
    fail_count: dict[str, int] = {}
    next_fire: dict[str, float] = {}

    try:
        while not _shutdown.is_set():
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
                _shutdown.wait(timeout=HEALTHCHECK_INTERVAL_DEFAULT)
                continue

            # Only check services whose next-fire has passed
            due = [name for name, nf in next_fire.items() if now >= nf]
            for name in due:
                hc = healthchecks.get(name)
                if not hc:
                    continue

                if hc.get("type") == "ping":
                    is_healthy = _run_ping_check(hc["host"], hc["timeout"])
                    check_info = f"ping {hc['host']}"
                elif hc.get("type") == "tcp":
                    is_healthy = _run_tcp_check(hc["host"], hc["port"], hc["timeout"])
                    check_info = f"tcp {hc['host']}:{hc['port']}"
                elif hc.get("type") == "soap":
                    is_healthy, code = _run_soap_check(
                        url=hc["url"], timeout=hc["timeout"],
                        soap_action=hc.get("soap_action", ""),
                        body=hc.get("body", ""),
                        healthy_codes=hc.get("healthy_codes"),
                        expected_string=hc.get("expected_string", ""),
                    )
                    check_info = f"soap code={code}"
                else:
                    code = _run_curl_check(hc["url"], hc["timeout"])
                    is_healthy = (code is not None and code in hc.get("healthy_codes", {200}))
                    check_info = f"code={code}"

                if is_healthy:
                    # Healthy — reset fail counter
                    if fail_count.get(name, 0) > 0:
                        print(f"Healthcheck OK [{name}] {check_info} (recovered)")
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
                              f"{check_info} -> {status}")

                # Schedule next check for this service on its own interval
                next_fire[name] = time.time() + hc["interval"]

            # Sleep until the next service is due
            if next_fire:
                min_next = min(next_fire.values())
                sleep_dt = max(0.1, min_next - time.time())
                _shutdown.wait(timeout=sleep_dt)
            else:
                _shutdown.wait(timeout=HEALTHCHECK_INTERVAL_DEFAULT)

    finally:
        # Release lock on shutdown
        _shutdown.set()
        try:
            lock_file.close()
        except Exception:
            pass


def run_healthchecks_once() -> dict[str, dict]:
    """Public entry-point: runs all healthchecks once (no DB mutation)."""
    healthchecks = _parse_healthchecks()
    results: dict[str, dict] = {}

    for name, hc in healthchecks.items():
        if hc.get("type") == "ping":
            healthy = _run_ping_check(hc["host"], hc["timeout"])
            results[name] = {"type": "ping", "host": hc["host"], "healthy": healthy}
        elif hc.get("type") == "tcp":
            healthy = _run_tcp_check(hc["host"], hc["port"], hc["timeout"])
            results[name] = {"type": "tcp", "host": hc["host"], "port": hc["port"], "healthy": healthy}
        elif hc.get("type") == "soap":
            is_healthy, code = _run_soap_check(
                url=hc["url"], timeout=hc["timeout"],
                soap_action=hc.get("soap_action", ""),
                body=hc.get("body", ""),
                healthy_codes=hc.get("healthy_codes"),
                expected_string=hc.get("expected_string", ""),
            )
            results[name] = {"type": "soap", "status_code": code, "healthy": is_healthy}
        else:
            code = _run_curl_check(hc["url"], hc["timeout"])
            healthy = (code is not None and code in hc.get("healthy_codes", {200}))
            results[name] = {"type": "curl", "status_code": code, "healthy": healthy}

    return results


def start_healthchecks():
    """Start the healthcheck background daemon thread if not already running."""
    global _HEALTHCHECK_THREAD
    with _HEALTHCHECK_START_LOCK:
        if _HEALTHCHECK_THREAD is not None and _HEALTHCHECK_THREAD.is_alive():
            return
        t = threading.Thread(target=_healthcheck_worker, daemon=True, name="healthcheck")
        t.start()
        _HEALTHCHECK_THREAD = t
