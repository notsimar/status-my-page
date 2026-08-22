"""Request logging for status-my-page.

Adds a structured access log line for every request, including:
  - client IP (X-Forwarded-For aware, falls back to remote_addr)
  - browser info: User-Agent (browser/platform summary + raw UA)
  - method, path, status code, duration, response size

Also routes the app's logger to logs/app.log with the same format so
warnings/errors carry timestamps and context instead of vanishing into
gunicorn's stderr.

Log files live in the instance/logs directory and rotate at 5 MB
(3 backups) so a busy page can't fill the disk.
"""

import logging
import logging.handlers
import time
from pathlib import Path

from flask import request

from statuspage.config import get_base_dir

_LOG_DIR: Path | None = None

# ── Formatter ───────────────────────────────────────────────────────

_LOG_FORMAT = (
    "%(asctime)s %(levelname)-8s %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _setup_logger(name: str, filename: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:  # idempotent under gunicorn workers
        handler = logging.handlers.RotatingFileHandler(
            _LOG_DIR / filename, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        logger.addHandler(handler)
        logger.propagate = False
    return logger


access_logger: logging.Logger | None = None
app_logger: logging.Logger | None = None


def init_logging() -> None:
    """Create the log directory and loggers. Called once from app.py."""
    global access_logger, app_logger, _LOG_DIR
    _LOG_DIR = get_base_dir() / "logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    access_logger = _setup_logger("statuspage.access", "access.log")
    app_logger = _setup_logger("statuspage.app", "app.log")


# ── Request info extraction ─────────────────────────────────────────

def client_ip() -> str:
    """Best-effort client IP: honours X-Forwarded-For behind a proxy.

    Takes the LEFTMOST forwarded entry (the original client). Only trust
    this if a reverse proxy sets the header; direct connections use
    remote_addr which cannot be spoofed.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "-"


def browser_summary() -> str:
    """Compact browser/platform summary parsed from the User-Agent."""
    ua = request.headers.get("User-Agent", "")
    if not ua:
        return "-"

    browser = "Other"
    for name, markers in (
        ("Firefox", ("Firefox/",)),
        ("Edge", ("Edg/", "Edge/")),
        ("Safari", ("Safari/",)),
        ("Chrome", ("Chrome/", "Chromium")),
        ("curl", ("curl/",)),
    ):
        if any(m in ua for m in markers):
            browser = name
            break

    os_name = "Unknown-OS"
    for name, marker in (
        ("Windows", "Windows"),
        ("Android", "Android"),
        ("iOS", "iPhone"),
        ("macOS", "Macintosh"),
        ("Linux", "Linux"),
    ):
        if marker in ua:
            os_name = name
            break

    return f"{browser}/{os_name}"


# ── Flask hooks ─────────────────────────────────────────────────────

def register_request_logging(app) -> None:
    """Attach before/after request hooks to the Flask app."""

    @app.before_request
    def _log_start_timer():
        request._sp_start_time = time.perf_counter()

    @app.after_request
    def _log_request(response):
        if access_logger is None:
            return response  # logging not initialized (e.g. bare test client)

        duration_ms = 0.0
        start = getattr(request, "_sp_start_time", None)
        if start is not None:
            duration_ms = (time.perf_counter() - start) * 1000

        ua = request.headers.get("User-Agent", "-")[:200]
        access_logger.info(
            '%s %s "%s %s" %d %.1fms %db ua="%s" [%s]',
            client_ip(),
            browser_summary(),
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            response.calculate_content_length() or 0,
            ua,
            client_ip(),  # repeated at end for easy awk '{print $NF}' extraction
        )
        return response
