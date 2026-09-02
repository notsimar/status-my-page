#!/usr/bin/env python3
"""Healthcheck probing module for status-my-page.

Contains individual check runners (_run_ping_check, _run_tcp_check,
_run_soap_check, _run_curl_check, _run_rss_feed_check) and the
_probe_result helper that runs one check and returns structured info.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import sqlite3
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

# Default healthcheck settings
HEALTHCHECK_INTERVAL_DEFAULT = 60
HEALTHCHECK_TIMEOUT_DEFAULT = 10
HEALTHCHECK_RETRIES_DEFAULT = 2
CURL_MAX_REDIRS = 5

# RSS feed check limits
RSS_MAX_ITEMS = 20
RSS_MAX_BYTES = 512 * 1024

# DOCTYPE with an internal DTD entity block — the classic "billion laughs"
# entity-expansion vector.
_RSS_DOCTYPE_INTERNAL_ENTITY = re.compile(
    r'<!DOCTYPE\s+\S+\s*\[\s*<!ENTITY[^>]+>', re.IGNORECASE
)


def feed_treats_as_unfetchable(body: str) -> bool:
    """True when a fetched feed body is a DTD entity-expansion (billion
    laughs) vector and must be treated as a fetch failure."""
    return bool(_RSS_DOCTYPE_INTERNAL_ENTITY.search(body))


def severity_from_failures(attempts: int, retries: int) -> str:
    """Failures → severity rule: >=retries*3 -> red, else degraded."""
    if attempts >= retries * 3:
        return "red"
    return "degraded"


# ── Safe validators (shared with parsing module) ───────────────────

def _safe_host(target: str) -> bool:
    """Allow valid IPv4/IPv6 addresses and hostnames for ping checks."""
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
    """Allow only http:// and https:// URLs."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except Exception:
        return False


def _safe_port(port: int) -> bool:
    """Validate port number is in valid range."""
    return isinstance(port, int) and 1 <= port <= 65535


# ── Individual check runners ───────────────────────────────────────

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
    """Run TCP connection check and return True if port is open."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


DEFAULT_SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body/></soap:Envelope>'
)


def _run_soap_check(
    url: str,
    timeout: int,
    soap_action: str = "",
    body: str = "",
    healthy_codes: set | None = None,
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
        "-L", "--",
        url,
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


def _run_curl_check(url: str, timeout: int, failure_keyword: str = "", degraded_keyword: str = "") -> tuple[bool, int | None, str]:
    """Run curl and return (is_healthy, status_code, result_status).

    result_status is one of:
    - "green": healthy status code, no failure/degraded keywords
    - "degraded": degraded_keyword found in response body
    - "red": non-healthy status code, curl failure, or failure_keyword found in response body
    """
    cmd = [
        "curl", "-s", "-o", "-", "-w", "\n%{http_code}",
        "--proto-default", "http",
        "--proto-redir", "-all,http,https",
        "--max-time", str(timeout),
        "--max-redirs", str(CURL_MAX_REDIRS),
        "-L", "--",
        url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=max(timeout + 5, CURL_MAX_REDIRS * 2 + 5),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, None, "red"

    stdout = result.stdout or ""
    if "\n" not in stdout:
        return False, None, "red"

    parts = stdout.rsplit("\n", 1)
    resp_body, code_str = parts[0], parts[1].strip()

    if not code_str.isdigit():
        return False, None, "red"

    code = int(code_str)
    if code == 0 or code > 599:
        return False, None, "red"

    # Red/failure keyword check has highest precedence
    if failure_keyword and isinstance(failure_keyword, str) and failure_keyword.strip():
        if failure_keyword.strip().lower() in resp_body.lower():
            return False, code, "red"

    # Degraded keyword check
    if degraded_keyword and isinstance(degraded_keyword, str) and degraded_keyword.strip():
        if degraded_keyword.strip().lower() in resp_body.lower():
            return False, code, "degraded"

    return True, code, "green"


def _run_rss_feed_check(url: str, timeout: int, keywords: dict | None = None) -> tuple[str | None, int | None]:
    """Fetch an RSS/Atom feed and scan recent entries for status keywords.

    Returns (result, http_code) where result is one of:
    - "red": the feed fetched and a "red" keyword matched
    - "degraded": no red match, but a "degraded" keyword matched
    - "green": the feed fetched cleanly with no keyword matches
    - None: fetch failed (non-http status, timeout, malformed XML)

    keywords maps "red"/"degraded" to lists of lower-case marker words
    (case-insensitive substring scan over each entry's title + description/summary).
    Only the first RSS_MAX_ITEMS entries are scanned, and
    curl --max-filesize rejects feeds larger than RSS_MAX_BYTES.
    """
    if keywords is None:
        keywords = {"red": [], "degraded": []}

    red_words = [w for w in keywords.get("red", []) if w]
    degraded_words = [w for w in keywords.get("degraded", []) if w]

    cmd = [
        "curl", "-s", "-o", "-", "-w", "\n%{http_code}",
        "--proto-default", "http",
        "--proto-redir", "-all,http,https",
        "--max-time", str(timeout),
        "--max-redirs", str(CURL_MAX_REDIRS),
        "--max-filesize", str(RSS_MAX_BYTES),
        "-L", "--",
        url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=max(timeout + 5, CURL_MAX_REDIRS * 2 + 5),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None, None

    stdout = result.stdout or ""
    if "\n" not in stdout:
        return None, None

    parts = stdout.rsplit("\n", 1)
    resp_body, code_str = parts[0], parts[1].strip()

    if not code_str.isdigit():
        return None, None

    code = int(code_str)
    if code == 0 or code > 599:
        return None, None

    # Reject DTD entity-expansion payloads BEFORE parsing (billion laughs).
    if feed_treats_as_unfetchable(resp_body):
        return None, code

    try:
        root = ET.fromstring(resp_body)
    except ET.ParseError:
        return None, code

    # Collect entry title/description/summary text for RSS (<item>) and
    # Atom (<entry>). Local-name matching keeps it namespace-agnostic.
    def _local(tag) -> str:
        return tag.rpartition("}")[2] if isinstance(tag, str) else ""

    texts = []
    entry_count = 0
    for entry in root.iter():
        if _local(entry.tag) not in ("item", "entry"):
            continue
        entry_count += 1
        if entry_count > RSS_MAX_ITEMS:
            break
        for el in entry:
            if _local(el.tag) in ("title", "description", "summary") and el.text:
                texts.append(el.text)

    hay = "\n".join(texts).lower()
    if red_words and any(w in hay for w in red_words):
        return "red", code
    if degraded_words and any(w in hay for w in degraded_words):
        return "degraded", code
    return "green", code