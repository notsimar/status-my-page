#!/usr/bin/env python3
"""Healthcheck worker module for status-my-page.

Background thread that polls each service on its own interval.
Uses _parse_healthchecks() from parsing module and individual check runners
from probing module. State mutations (fail counters, status flips) happen
on the caller's thread after all probes finish.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

from statuspage.constants import MAX_HISTORY_PER_ITEM

from statuspage._healthcheck_parsing import (
    _parse_healthchecks,
    _safe_host,
    _safe_url,
    _safe_port,
    _get_max_history_per_item,
    HEALTHCHECK_INTERVAL_DEFAULT,
)
from statuspage._healthcheck_probing import (
    _run_ping_check,
    _run_tcp_check,
    _run_soap_check,
    _run_curl_check,
    _run_rss_feed_check,
    _set_health_status,
    severity_from_failures,
)


# ── Module-level state ─────────────────────────────────────────────

_HEALTH_LOCK = threading.Lock()
_HEALTHCHECK_THREAD: object | None = None
_HEALTHCHECK_START_LOCK = threading.Lock()

_BASE_DIR: Path | None = None
_DB_PATH: Path | None = None
_CONFIG_PATH: Path | None = None
_LOAD_CONFIG = None
_MAX_HISTORY_PER_ITEM = MAX_HISTORY_PER_ITEM


def configure_healthcheck(
    base_dir: Path,
    db_path: Path,
    config_path: Path,
    load_config_fn,
    max_history_per_item: int = MAX_HISTORY_PER_ITEM,
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
    import sqlite3
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _set_health_status(name: str, desired_status: str) -> None:
    """Set a DB item's status to match the healthcheck result (green/degraded/red).

    Uses its own DB connection (_health_db) — safe outside Flask request context.
    Writes are serialized via _HEALTH_LOCK to avoid conflicting with Flask threads.
    """
    with _HEALTH_LOCK:
        queue_slack = False
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
                "UPDATE status_items SET status = ? WHERE id = ?",
                [desired_status, row["id"]],
            )

            # Record history (actor omitted — healthcheck entries have no user source)
            try:
                conn.execute(
                    "INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) "
                    "VALUES (?, 'status', ?, ?, ?)",
                    (row["id"], current, desired_status, ts),
                )
                # Queue Slack notification (best-effort; never blocks the flip).
                # Deferred until AFTER conn.commit() below — enqueuing inside
                # this open transaction would self-deadlock on the DB write lock.
                queue_slack = True
                # Prune to the last N rows FOR THIS ITEM only. (Without the
                # outer item_id filter the subquery only contains this item's
                # ids, so NOT IN would wipe every other item's history the
                # moment this item flips — breaking multi-service feeds.)
                conn.execute(
                    "DELETE FROM status_history WHERE item_id = ? AND id NOT IN ("
                    "  SELECT id FROM status_history WHERE item_id = ? ORDER BY id DESC LIMIT ?"
                    ")",
                    (row["id"], row["id"], _get_max_history_per_item()),
                )
            except Exception:
                # Non-fatal: a history write/prune hiccup must not drop the
                # status flip (main UPDATE already applied). Log so a
                # persistent failure (e.g. schema drift) is visible.
                print(
                    f"Healthcheck warning: could not record history for "
                    f"{name!r} -> {desired_status!r} (status update still applied)"
                )

            conn.commit()
        finally:
            conn.close()

        # Slack enqueue AFTER the transaction is committed and the connection
        # released, so the outbox write never contends with the flip's own lock.
        if queue_slack:
            try:
                from statuspage.slack import enqueue_status_change
                enqueue_status_change(name, current, desired_status, ts)
            except Exception as exc:
                print(f"Slack warning: queue failed for {name!r} ({exc})")


def _probe_result(name: str, hc: dict) -> dict:
    """Run ONE healthcheck probe (no DB writes, no shared-state mutation).

    Returns {"name", "svc_name", "is_healthy", "check_info",
    "immediate_status"}. ``immediate_status`` is set when the probe result
    maps directly to a status (rss red/degraded, curl degraded keyword)
    instead of going through the retry counter.
    """
    svc_name = hc.get("service") or name
    immediate = None

    hc_type = hc.get("type")

    if hc_type == "rss":
        from statuspage._healthcheck_probing import _run_rss_feed_check
        rss_result, code = _run_rss_feed_check(
            url=hc["url"], timeout=hc["timeout"], keywords=hc.get("keywords"),
        )
        check_info = f"rss code={code} result={rss_result}"
        if rss_result in ("red", "degraded"):
            immediate = rss_result
        is_healthy = (rss_result == "green")

    elif hc_type == "ping":
        is_healthy = _run_ping_check(hc["host"], hc["timeout"])
        check_info = f"ping {hc['host']}"

    elif hc_type == "tcp":
        is_healthy = _run_tcp_check(hc["host"], hc["port"], hc["timeout"])
        check_info = f"tcp {hc['host']}:{hc['port']}"

    elif hc_type == "soap":
        is_healthy, code = _run_soap_check(
            url=hc["url"], timeout=hc["timeout"],
            soap_action=hc.get("soap_action", ""),
            body=hc.get("body", ""),
            healthy_codes=hc.get("healthy_codes"),
            expected_string=hc.get("expected_string", ""),
        )
        check_info = f"soap code={code}"

    else:  # curl
        body_ok, code, res_status = _run_curl_check(
            hc["url"], hc["timeout"],
            failure_keyword=hc.get("failure_keyword", ""),
            degraded_keyword=hc.get("degraded_keyword", ""),
        )
        is_healthy = (code is not None and code in hc.get("healthy_codes", {200}) and body_ok)
        check_info = f"code={code}"
        if not is_healthy and res_status == "degraded":
            immediate = "degraded"

    return {
        "name": name,
        "svc_name": svc_name,
        "is_healthy": is_healthy,
        "check_info": check_info,
        "immediate_status": immediate,
    }


def _run_due_checks(healthchecks: dict, due: list, fail_count: dict, next_fire: dict) -> None:
    """Probe all due services concurrently, then apply results serially.

    Probes run in a bounded ThreadPoolExecutor (each is a subprocess or
    socket with its own timeout) so one slow endpoint cannot delay every
    other service's interval. State mutations (fail counters, status flips,
    next-fire scheduling) happen back on the caller's thread after all
    probes finish — same semantics as the previous serial loop.
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [(name, healthchecks[name]) for name in due if name in healthchecks]
    if not targets:
        return

    HC_PROBE_POOL_SIZE = 8
    with ThreadPoolExecutor(max_workers=min(HC_PROBE_POOL_SIZE, max(1, len(targets)))) as pool:
        results = list(pool.map(lambda t: _probe_result(t[0], t[1]), targets))

    for res in results:
        name = res["name"]
        hc = healthchecks[name]
        svc_name = res["svc_name"]
        check_info = res["check_info"]

        # Direct-mapped statuses bypass the retry counter entirely.
        if res["immediate_status"]:
            fail_count[name] = 0
            _set_health_status(svc_name, res["immediate_status"])
            next_fire[name] = time.time() + hc["interval"]
            continue

        if res["is_healthy"]:
            # Healthy — reset fail counter
            if fail_count.get(name, 0) > 0:
                print(f"Healthcheck OK [{name} -> {svc_name}] {check_info} (recovered)")
            fail_count[name] = 0
            _set_health_status(svc_name, "green")
        else:
            # Unhealthy — increment counter. Severity per
            # severity_from_failures(): >=retries failures -> degraded,
            # >=3*retries -> red (below the threshold it stays as-is).
            fail_count[name] = fail_count.get(name, 0) + 1
            attempts = fail_count[name]
            threshold = hc["retries"]

            if attempts >= threshold:
                status = severity_from_failures(attempts, threshold)
                _set_health_status(svc_name, status)
                print(
                    f"Healthcheck FAIL [{name} -> {svc_name}] "
                    f"consecutive_failures={attempts} threshold={threshold} "
                    f"(red at {threshold * 3}) {check_info} -> {status}"
                )

        # Schedule next check for this service on its own interval
        next_fire[name] = time.time() + hc["interval"]


def _healthcheck_worker(stop_event: threading.Event | None = None) -> None:
    """Background thread that polls each service on its own interval.

    A file-based advisory lock (instance/.healthcheck.lock, fcntl) ensures only
    one worker process runs the loop at a time. The worker holds NO persistent
    DB connection: it opens a WAL connection only briefly when flipping a
    status, so it never contends with admin writes or DB rebuilds.

    An optional ``stop_event`` lets callers (tests) halt the loop cleanly; the
    app's own worker runs without one and lives for the process lifetime.
    """
    _shutdown = stop_event if stop_event is not None else threading.Event()

    if _DB_PATH is None:
        raise RuntimeError("Healthcheck not configured. Call configure_healthcheck() first.")

    # Acquire file lock - only one worker process runs the healthcheck worker
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
    except Exception as e:
        # Couldn't even open the lock file — proceeding unlocked means two
        # worker processes could briefly race; log it so it's diagnosable.
        print(
            f"Healthcheck warning: could not open lock file at "
            f"{lock_file_path} ({e!r}); proceeding unlocked"
        )
        lock_file = None  # couldn't even open the lock file; proceed unlocked

    # Track consecutive failures per service (for retry/backoff)
    fail_count: dict[str, int] = {}
    next_fire: dict[str, float] = {}

    try:
        while not _shutdown.is_set():
            # ── Reload config every cycle so changes without restart work ──
            cfg = _LOAD_CONFIG() if callable(_LOAD_CONFIG) else {}
            sec = cfg.get("settings") if isinstance(cfg, dict) else {}
            if isinstance(sec, dict) and not sec.get("healthchecks_enabled", True):
                # Healthchecks toggled off via settings
                _shutdown.wait(timeout=HEALTHCHECK_INTERVAL_DEFAULT)
                continue

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

            # Only check services whose next-fire has passed. Each due check
            # runs in a worker thread (bounded pool) so one slow endpoint's
            # full timeout can't stall every other service's interval. The
            # per-name state dicts are only touched after all probes finish,
            # from this thread, so no extra locking is needed.
            due = [name for name, nf in next_fire.items() if now >= nf]
            _run_due_checks(healthchecks, due, fail_count, next_fire)

            # Sleep until the next service is due
            if next_fire:
                min_next = min(next_fire.values())
                sleep_dt = max(0.1, min_next - time.time())
                _shutdown.wait(timeout=sleep_dt)
            else:
                _shutdown.wait(timeout=HEALTHCHECK_INTERVAL_DEFAULT)

    finally:
        # Release the lock; the stop event (if any) stays caller-owned.
        try:
            lock_file.close()
        except Exception as e:
            # Cleanup-only failure (worker loop has already exited); log so
            # persistent close errors (disk health, permissions) are visible.
            print(f"Healthcheck warning: could not close lock file: {e!r}")


def run_healthchecks_once() -> dict[str, dict]:
    """Public entry-point: runs all healthchecks once (no DB mutation).

    Bounded by design (this is the one-shot ``POST /api/healthcheck/run``
    path, which blocks an HTTP request):
      * per-check timeout is capped at HEALTHCHECK_ONE_SHOT_TIMEOUT_CAP
        (the background worker deliberately keeps its own, larger, config
        values — they are passed by reference and never mutated here)
      * a hard overall wall-clock budget of HEALTHCHECK_RUN_HARD_TIMEOUT
        seconds: checks that would not finish in time are reported as
        ``"timed_out": true`` rather than stalling the request.
    """
    from statuspage.constants import HEALTHCHECK_RUN_HARD_TIMEOUT, HEALTHCHECK_ONE_SHOT_TIMEOUT_CAP

    healthchecks = _parse_healthchecks()
    results: dict[str, dict] = {}

    deadline = time.monotonic() + HEALTHCHECK_RUN_HARD_TIMEOUT
    for name, hc in healthchecks.items():
        if time.monotonic() >= deadline:
            # Budget exhausted — skip the remainder rather than stall.
            results[name] = {
                "type": hc.get("type", ""),
                "healthy": False,
                "timed_out": True,
                "detail": "one-shot run time budget exhausted",
            }
            continue

        remaining = max(1, int(deadline - time.monotonic()))
        timeout = min(int(hc.get("timeout", HEALTHCHECK_TIMEOUT_DEFAULT)),
                      HEALTHCHECK_ONE_SHOT_TIMEOUT_CAP, remaining)

        if hc.get("type") == "ping":
            healthy = _run_ping_check(hc["host"], timeout)
            results[name] = {"type": "ping", "host": hc["host"], "healthy": healthy}
        elif hc.get("type") == "tcp":
            healthy = _run_tcp_check(hc["host"], hc["port"], timeout)
            results[name] = {"type": "tcp", "host": hc["host"], "port": hc["port"], "healthy": healthy}
        elif hc.get("type") == "soap":
            from statuspage._healthcheck_probing import _run_soap_check
            is_healthy, code = _run_soap_check(
                url=hc["url"], timeout=timeout,
                soap_action=hc.get("soap_action", ""),
                body=hc.get("body", ""),
                healthy_codes=hc.get("healthy_codes"),
                expected_string=hc.get("expected_string", ""),
            )
            results[name] = {"type": "soap", "status_code": code, "healthy": is_healthy}
        elif hc.get("type") == "rss":
            from statuspage._healthcheck_probing import _run_rss_feed_check
            rss_result, code = _run_rss_feed_check(
                url=hc["url"], timeout=timeout, keywords=hc.get("keywords"),
            )
            results[name] = {"type": "rss", "url": hc["url"],
                             "status_code": code, "result": rss_result,
                             "healthy": rss_result == "green"}
        else:  # curl
            from statuspage._healthcheck_probing import _run_curl_check
            body_ok, code, res_status = _run_curl_check(
                hc["url"], timeout,
                failure_keyword=hc.get("failure_keyword", ""),
                degraded_keyword=hc.get("degraded_keyword", ""),
            )
            healthy = (code is not None and code in hc.get("healthy_codes", {200}) and body_ok)
            results[name] = {"type": "curl", "status_code": code, "result": res_status, "healthy": healthy}

    return results


def start_healthchecks() -> None:
    """Start the healthcheck background daemon thread if not already running."""
    global _HEALTHCHECK_THREAD
    with _HEALTHCHECK_START_LOCK:
        if _HEALTHCHECK_THREAD is not None and _HEALTHCHECK_THREAD.is_alive():
            return
        from statuspage._healthcheck_parsing import configure_healthcheck
        # Configure will be called again if needed by the app
        t = threading.Thread(target=_healthcheck_worker, daemon=True, name="healthcheck")
        t.start()
        _HEALTHCHECK_THREAD = t