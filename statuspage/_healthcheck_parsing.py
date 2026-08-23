#!/usr/bin/env python3
"""Healthcheck parsing module for status-my-page.

Relods healthchecks from config.yaml on every call so edits take effect.
Handles type auto-detection and sanitization of numeric fields.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from pathlib import Path

# Default healthcheck settings
HEALTHCHECK_INTERVAL_DEFAULT = 60
HEALTHCHECK_TIMEOUT_DEFAULT = 10
HEALTHCHECK_RETRIES_DEFAULT = 2
CURL_MAX_REDIRS = 5

# RSS feed check limits
RSS_MAX_ITEMS = 20
RSS_MAX_BYTES = 512 * 1024

# DOCTYPE with an internal DTD entity block — the classic "billion laughs"
# entity-expansion vector. Paid/vendor RSS never needs internal entities, so
# any feed presenting one is rejected as a fetch failure (never as green).
_RSS_DOCTYPE_INTERNAL_ENTITY = re.compile(
    r'<!DOCTYPE\s+\S+\s*\[\\s*<!ENTITY[^>]+>', re.IGNORECASE
)


def feed_treats_as_unfetchable(body: str) -> bool:
    """True when a fetched feed body is a DTD entity-expansion (billion
    laughs) vector and must be treated as a fetch failure. See
    _RSS_DOCTYPE_INTERNAL_ENTITY for the rule. Exported for tests + docs.
    """
    return bool(_RSS_DOCTYPE_INTERNAL_ENTITY.search(body))


def severity_from_failures(attempts: int, retries: int) -> str:
    """The failures → severity rule for an *unhealthy* result, in one place.

        retries <= attempts < retries*3 -> "degraded"
        attempts >= retries*3           -> "red"

    Callers only invoke this once attempts >= retries (the worker loop
    checks that first); anything below is unreachable by contract.
    """
    if attempts < retries:
        raise ValueError(
            f"severity_from_failures called below retry threshold "
            f"(attempts={attempts} < retries={retries})"
        )
    if attempts >= retries * 3:
        return "red"
    return "degraded"


# ── Configuration (set by app.configure_healthcheck()) ─────────────

_BASE_DIR: object | None = None
_DB_PATH: object | None = None
_CONFIG_PATH: object | None = None
_LOAD_CONFIG: object | None = None
_MAX_HISTORY_PER_ITEM = 100


def configure_healthcheck(
    base_dir,
    db_path,
    config_path,
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


def _get_max_history_per_item() -> int:
    return _MAX_HISTORY_PER_ITEM


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
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except Exception:
        return False


def _safe_port(port: int) -> bool:
    """Validate port number is in valid range."""
    return isinstance(port, int) and 1 <= port <= 65535


# ── Parsing ────────────────────────────────────────────────────────

VALID_HC_TYPES = ["curl", "ping", "tcp", "rss"]


def _parse_healthchecks() -> dict[str, dict]:
    """Reload healthchecks from config.yaml on every call so edits take effect.

    Returns dict of name -> healthcheck config, with sanitized/normalized fields.
    """
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
        # Support alternate key names from older configs
        soap_action = details.get("soap_action", details.get("action", ""))
        soap_body = details.get("body", details.get("envelope", ""))
        expected_string = details.get("expected_string", details.get("expected", ""))
        failure_keyword = details.get("failure_keyword", details.get("failure_string", details.get("fail_keyword", "")))
        degraded_keyword = details.get("degraded_keyword", details.get("degraded_string", details.get("deg_keyword", "")))
        target_service = details.get("service", details.get("service_name", details.get("item", details.get("item_name", ""))))
        target_service = str(target_service).strip() if target_service else ""

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

        # --- type-specific building ---

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
                "service": target_service or name.strip(),
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
                "service": target_service or name.strip(),
                "interval": max(interval, 1),
                "timeout": max(timeout_val, 1),
                "retries": max(retries, 1),
            }

        elif check_type == "rss":
            # Explicit type only (no auto-detect: a bare url means curl).
            if not url or not isinstance(url, str) or not url.strip():
                continue
            if not _safe_url(url.strip()):
                continue

            # Keyword map: level -> lower-case marker words scanned in the
            # feed. Empty lists are legal (then a fetch failure is the only
            # signal of unhealthiness).
            raw_kw = details.get("keywords", {}) or {}
            keywords: dict[str, list[str]] = {"red": [], "degraded": []}
            if isinstance(raw_kw, dict):
                for level in ("red", "degraded"):
                    raw_level = raw_kw.get(level, [])
                    words: list[str] = []
                    if isinstance(raw_level, str):
                        raw_level = [raw_level]
                    if isinstance(raw_level, list):
                        for w in raw_level:
                            if w is None:
                                continue
                            w = str(w).strip().lower()
                            if w:
                                words.append(w)
                    keywords[level] = words

            healthchecks[name.strip()] = {
                "type": "rss",
                "url": url.strip(),
                "keywords": keywords,
                "service": target_service or name.strip(),
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
                    "failure_keyword": str(failure_keyword).strip() if failure_keyword else "",
                    "degraded_keyword": str(degraded_keyword).strip() if degraded_keyword else "",
                    "service": target_service or name.strip(),
                    "interval": max(interval, 1),
                    "timeout": max(timeout_val, 1),
                    "retries": max(retries, 1),
                    "healthy_codes": codes,
                }
            else:
                healthchecks[name.strip()] = {
                    "type": "curl",
                    "url": url.strip(),
                    "failure_keyword": str(failure_keyword).strip() if failure_keyword else "",
                    "degraded_keyword": str(degraded_keyword).strip() if degraded_keyword else "",
                    "service": target_service or name.strip(),
                    "interval": max(interval, 1),
                    "timeout": max(timeout_val, 1),
                    "retries": max(retries, 1),
                    "healthy_codes": codes,
                }

    return healthchecks