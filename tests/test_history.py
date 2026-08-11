#!/usr/bin/env python3
"""Test suite for the status history feature.

Verifies:
  - GET /api/history/<id> returns JSON with service name + entries array
  - Empty history before any mutations gives an empty entries list
  - Toggle status creates a history record (event_type='status')
  - Update notes creates a history record (event_type='notes')
  - Each entry has event_type, old_value, new_value, occurred fields
  - Entries returned newest-first (DESC order)
  - Non-existent item_id returns 404
  - Multiple toggles all recorded with correct transitions
  - History endpoint is publicly accessible without auth

Usage:
    # Server must be running on http://localhost:8920
    cd /home/ssahni/Developer/status-my-page && python3 tests/test_history.py [http://URL]
"""

import datetime as dt
import http.client
import http.cookiejar
import json
import re
import sys
import time
import urllib.request
from typing import Any, Optional


BASE: str = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8920"
HOST: str = BASE.replace("http://", "").split("/")[0].split(":")[0] or "localhost"
PORT: int = int(BASE.split(":")[2].split("/")[0]) if ":" in BASE.split("//")[1] and not BASE.split(":")[2].startswith("/") else 8920
# Simpler host/port extraction
host_port = BASE.replace("http://", "").rstrip("/")
if ":" in host_port:
    parts = host_port.rsplit(":", 1)
    HOST = parts[0]
    PORT = int(parts[1])
else:
    HOST = host_port if host_port else "localhost"
    PORT = 8920

ADDED_SERVICE = "TestHistorySvc_" + str(int(time.time()))
_CSRF_TOKEN: str = ""
PASS_HASH: str = sys.argv[2] if len(sys.argv) > 2 else "__NONE__"

# Cookie jar for session persistence
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


# ── HTTP helpers ─────────────────────────────────────────────────────


def request(method: str, path: str, body: Optional[dict] = None) -> tuple[int, Any]:
    """Send a request with cookie persistence and CSRF header."""
    global _CSRF_TOKEN
    url = BASE + path
    data_body = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data_body, method=method)
    req.add_header("Content-Type", "application/json")
    if _CSRF_TOKEN and method != "GET":
        req.add_header("X-CSRF-Token", _CSRF_TOKEN)

    try:
        with opener.open(req, timeout=10) as resp:
            raw = resp.read().decode()
            ct = resp.headers.get("Content-Type", "")
            parsed = json.loads(raw) if "application/json" in ct else raw
            # Auto-rotate CSRF token after successful mutations (mirrors JS csrfFetch)
            if resp.status == 200 and method != "GET":
                try:
                    with opener.open(
                        urllib.request.Request(BASE + "/api/csrf-token"), timeout=10
                    ) as tok_resp:
                        tok_raw = json.loads(tok_resp.read().decode())
                        if "token" in tok_raw:
                            _CSRF_TOKEN = tok_raw["token"]
                except Exception:
                    pass  # non-critical — token refreshes on next page load
            return resp.status, parsed  # type: ignore[return-value]
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"error": raw}
        return e.code, parsed


def login(username: str, password: str) -> bool:
    """Login and return success flag."""
    status, _body = request("POST", "/login", {"user": username, "pass": password})
    return status == 200


def get_csrf_from_page() -> str:
    """Extract CSRF token from the rendered page HTML."""
    _status, html_raw = request("GET", "/")
    if isinstance(html_raw, str):
        match = re.search(r'window\.__CSRF__\s*=\s*["\x27]([a-f0-9]+)["\x27]', html_raw)
        return match.group(1) if match else ""
    return ""


def get_id_by_name(item_name: str) -> Optional[int]:
    """Find an item's DB id by its display name from page HTML."""
    _status, html_raw = request("GET", "/")
    if not isinstance(html_raw, str):
        return None
    pattern = (
        r'<div\s+class="status-row"\s+data-id="(\d+)"[^>]+>'
        r'.*?<span\s+class="status-name">'
        + re.escape(item_name)
        + r'</span>'
    )
    m = re.search(pattern, html_raw, re.DOTALL)
    return int(m.group(1)) if m else None


# ── Test runner helpers ────────────────────────────────────────────

passed = 0
failed = 0


def check(condition: bool, label: str) -> None:
    global passed, failed
    if condition:
        print(f"  \u2705 {label}")
        passed += 1
    else:
        print(f"  \u274c {label}")
        failed += 1


# ════════════════════════════════════════════════════════════════════
# Main test flow
# ════════════════════════════════════════════════════════════════════

print("\n" + "=" * 58)
print("  Status History Feature — Test Suite")
print("=" * 58)
print(f"Target: {BASE}\n")


# ── T0: Server reachable ────────────────────────────────────────────
status, _ = request("GET", "/")
check(status == 200, "T0: Server responds with HTTP 200")

# ── T1: Login ───────────────────────────────────────────────────────
if not login("admin", "changeme"):
    print("\n\u26a0 Cannot proceed — login failed.")
    sys.exit(1)
check(True, "T1: Admin login successful")

# Capture CSRF token
_CSRF_TOKEN = get_csrf_from_page()
check(bool(_CSRF_TOKEN), "T1b: CSRF token extracted from page")

# ── T2: Add a unique test service ───────────────────────────────────
_status, _body = request("POST", "/api/add", {"name": ADDED_SERVICE})
# Use the API return value directly — more reliable than re-parsing HTML
item_id = None
if isinstance(_body, dict) and "item" in _body:
    item_id = _body["item"]["id"]
check(item_id is not None, f"T2: Added test service '{ADDED_SERVICE}' (id={item_id})")

if item_id is None:
    print("\n\u26a0 Cannot proceed — service was not added.")
    sys.exit(1)

# ── T3: Initial history is empty ────────────────────────────────────
status, data = request("GET", f"/api/history/{item_id}")
check(status == 200, "T3a: History endpoint returns 200")
is_dict = isinstance(data, dict)
entries: list[dict[str, str]] = data.get("entries", []) if is_dict else []
check(is_dict and data.get("service") == ADDED_SERVICE,
      "T3b: Response contains correct service name")
check(isinstance(entries, list) and len(entries) == 0,
      "T3c: No history entries before any mutations")

# ── T4: Toggle status creates a record ─────────────────────────────
time.sleep(0.15)
request("POST", f"/api/toggle/{item_id}")  # green -> degraded
status, data = request("GET", f"/api/history/{item_id}")
entries = data.get("entries", []) if isinstance(data, dict) else []

has_toggle = any(
    e.get("event_type") == "status"
    and e.get("old_value") == "green"
    and e.get("new_value") == "degraded"
    for e in entries
)
check(has_toggle, "T4: Toggle created status history record (green->degraded)")

# ── T5: Entry has all required fields + valid timestamp ────────────
first_entry = next((e for e in entries if e.get("event_type") == "status"), None)
if first_entry:
    required = ["event_type", "old_value", "new_value", "occurred"]
    all_present = all(k in first_entry for k in required)
    check(all_present,
          f"T5a: Entry has all required fields {required}")

    raw_ts = first_entry.get("occurred", "")
    try:
        parsed_dt = dt.datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        check(parsed_dt.year >= 2024, f"T5b: Valid ISO-8601 timestamp ({raw_ts})")
    except (ValueError, TypeError):
        check(False, f"T5b: Invalid timestamp format '{raw_ts}'")

# ── T6: Notes update creates a record ───────────────────────────────
time.sleep(0.15)
test_note = "High latency reported, investigating"
request("POST", f"/api/notes/{item_id}", {"notes": test_note})
status, data = request("GET", f"/api/history/{item_id}")
entries = data.get("entries", []) if isinstance(data, dict) else []

has_notes_entry = any(
    e.get("event_type") == "notes"
    and e.get("new_value") == test_note
    for e in entries
)
check(has_notes_entry, "T6: Notes update created history record")

# ── T7: Entries ordered newest-first ────────────────────────────────
if len(entries) >= 2:
    ts_newer = dt.datetime.fromisoformat(entries[0]["occurred"].replace("Z", "+00:00"))
    ts_older = dt.datetime.fromisoformat(entries[1]["occurred"].replace("Z", "+00:00"))
    check(ts_newer >= ts_older, "T7: Entries returned newest-first (DESC order)")

# ── T8: Multiple toggles all recorded with correct transitions ───────
time.sleep(0.15)
request("POST", f"/api/toggle/{item_id}")  # degraded -> red
request("POST", f"/api/toggle/{item_id}")  # red -> green
status, data = request("GET", f"/api/history/{item_id}")
entries = data.get("entries", []) if isinstance(data, dict) else []

status_entries = [e for e in entries if e.get("event_type") == "status"]
check(len(status_entries) == 3,
      f"T8a: All 3 toggles recorded (got {len(status_entries)})")

transitions = set(
    (e["old_value"], e["new_value"]) for e in status_entries
)
expected = {("green", "degraded"), ("degraded", "red"), ("red", "green")}
check(transitions == expected,
      f"T8b: Correct state transitions captured")

# ── T9: Non-existent item returns 404 ───────────────────────────────
status, _ = request("GET", "/api/history/99999")
check(status == 404, "T9: Non-existent item_id returns 404")

# ── T10: Notes history stores distinct old/new values ───────────────
notes_entries = [e for e in entries if e.get("event_type") == "notes"]
if notes_entries:
    ne = notes_entries[0]
    check(ne["old_value"] != ne["new_value"],
          "T10: Notes history records distinct old vs new values")

# ── T11: History endpoint is public (no auth required) ─────────────
fresh_cj = http.cookiejar.CookieJar()
fresh_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(fresh_cj)
)

furl = BASE + f"/api/history/{item_id}"
req2 = urllib.request.Request(furl, method="GET")
try:
    with fresh_opener.open(req2, timeout=10) as resp2:
        fstatus = resp2.status
        fraw = resp2.read().decode()
        fdata = json.loads(fraw) if "json" in resp2.headers.get("Content-Type", "") else {}
except urllib.error.HTTPError as e:
    fstatus = e.code
    fraw = e.read().decode()
    try:
        fdata = json.loads(fraw)
    except Exception:
        fdata = {}

fentries = fdata.get("entries", []) if isinstance(fdata, dict) else []
check(fstatus == 200 and len(fentries) > 0,
      "T11: History accessible without admin auth (public read)")


# ── Cleanup ─────────────────────────────────────────────────────────
print("\n--- Cleanup ---")
request("POST", f"/api/delete/{item_id}")
remaining_id = get_id_by_name(ADDED_SERVICE)
check(remaining_id is None, "Cleanup: Test service removed successfully")


# ════════ Summary ════════
total = passed + failed
print(f"\n{'=' * 58}")
if failed:
    print(f"Results: {passed}/{total} passed, {failed} FAILED \u274c")
else:
    print(f"Results: {passed}/{total} passed \u2705 ALL PASSED")
print(f"{'=' * 58}\n")

sys.exit(1 if failed else 0)
