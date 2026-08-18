# Testing & Quality Assurance Guide

## Table of Contents

- [1. Test Categories](#1-test-categories)
- [2. Running Tests](#2-running-tests)
- [3. Unit Tests (Structural / MC/DC)](#3-unit-tests-structural--mcdc)
- [4. Functional Tests](#4-functional-tests)
- [5. Healthcheck Tests](#5-healthcheck-tests)
- [6. Regression & Integration](#6-regression--integration)
- [7. Adding New Tests](#7-adding-new-tests)

---

## 1. Test Categories

This project organizes tests into four categories:

### Unit (Structural / MC/DC) — Multiple test files

Prove that every guard condition in compound boolean expressions **independently** controls the outcome. These are not "happy-path" tests — they are designed to fail at a specific gate.

| Decision | Location | Expression | Test File | Coverage |
|----------|----------|------------|-----------|----------|
| D1: RestoreStatus | app.py L342 | `not in seed_set or state in ('green','')` | Test_D1_RestoreStatus (4 tests) | Full MC/DC |
| D2: RestoreNotes | app.py L355 | `not in seed_set or not note_text.strip()` | Test_D2_NotesRestore (3 tests) | Full MC/DC |
| D3: SecurityGate | app.py L680+ | `_not_admin() or not _check_csrf() or not _check_mutation_rate(ip)` | Test_D3_SecurityGuard (14 tests across 4 endpoints) | Full MC/DC |
| D4: ReorderOverride | app.py L395 | `reorder_list and isinstance(reorder_list, list)` | Test_D4_ReorderOverride (7 tests) | Full MC/DC |
| D5: SetNotesGuard | app.py L547 | `current_row and notes.strip()` | Test_D5_SetNotesGuard (7 tests) | Full MC/DC |
| D6: CsrfInternalGuard | app.py L648 | `not expected or not hmac.compare_digest(sent, expected)` | Test_D6_CsrfInternalGuard (4 tests) | Full MC/DC |
| D7: DeleteCleanupGate | app.py L851 | `"items" in rt and name in rt["items"]` | Test_D7_DeleteCleanupGate (5 tests) | Full MC/DC |
| D_hc1: HealthResultGate | healthcheck.py L222 | `code is not None and code in healthy_codes` | Test_Dhc1_HealthResultGate (3 tests) | Full MC/DC |
| D_hc2: UrlSanitisation | healthcheck.py L124 | `not url or not isinstance(url, str) or not url.strip()` | Test_Dhc2_UrlSanitisation (5 tests) | Full MC/DC |
| D_hc3: TypeAutoDetection | healthcheck.py L144 | soap→tcp→ping→curl inference chain | Test_Dhc3_TypeAutoDetection (6 tests) | Full MC/DC |
| D_hc5: TcpValidation | healthcheck.py L188 | host type + `_safe_host` + port range | Test_Dhc5_TcpValidation (8 tests) | Full MC/DC |
| D_hc7: SoapResultGate | healthcheck.py L323 | `code in healthy_codes and expected in body` | Test_Dhc7_SoapResultGate (4 tests) | Full MC/DC |
| D_hc8: RssResponseGate | healthcheck.py L447 | `\n` in stdout → isdigit → 1≤code≤599 → code==200 → no ParseError | Test_Dhc8_RssResponseGate (7 tests) | Full MC/DC |
| D_hc9: RssKeywordPrecedence | healthcheck.py L479 | `(red set & match) → red; (deg set & match) → deg; else green` | Test_Dhc9_RssKeywordPrecedence (6 tests) | Full MC/DC |
| D_hc10: RssUrlGuard | healthcheck.py L213 | `not url or not str or not url.strip() or not _safe_url` | Test_Dhc10_RssUrlGuard (5 tests) | Full MC/DC |
| D_hc11: RssEntryFilter | healthcheck.py L468 | item/entry tag + child tag local-name filter | Test_Dhc11_RssEntryFilter (3 tests) | Full MC/DC |

**Total: structural + healthcheck gates — 18 compound decisions, 100% decision coverage, all conditions MC/DC-proven (50 tests in test_healthcheck_mc_dc.py alone; D1–D7 in test_mc_dc.py / test_structural.py).**

### Functional — `tests/test_history.py` + `tests/test_routes_and_features.py`

End-to-end HTTP tests that verify the API behavior against a running server with a seeded database.

**test_history.py (13 scenarios):**
- **test_status_toggle**: Cycles status and records history entry
- **test_notes_update**: Writes notes and verifies persistence
- **test_history_newest_first**: Fetches history timeline, verifies reverse chronological order
- **test_fields_present**: Validates each history record has event_type, item_id, old_value, new_value, occurred
- **test_public_api_access**: Confirms /api/history/<id> is publicly readable
- **test_api_errors**: Verifies proper HTTP status codes for bad requests
- **test_cascade_delete**: Item deletion removes history from DB and YAML
- **test_pruning_cap**: History table capped at MAX_HISTORY_PER_ITEM

**test_routes_and_features.py:**
- Auth: login, logout, rate limiting, auth-check, CSRF token endpoints
- Mutations: add (409 conflict), rename, delete (404), reorder, toggle
- Security: headers, DB archive snapshots, backup rotation
- Admin credential validation (missing STATUS_ADMIN_PASS_HASH)

### Healthcheck — `tests/test_healthcheck.py` + `tests/test_healthcheck_mc_dc.py`

**test_healthcheck.py (44 tests):**
- URL scheme validation (http/https only)
- Host/IP validation for ping (rejects options, command injection)
- Config parsing: defaults, custom intervals, healthy_codes, SOAP auto-detection
- Real subprocess tests: ping localhost, curl connection refused, curl binary present
- Healthcheck endpoints: GET /api/healthchecks, POST /api/healthcheck/run (admin+CSRF)
- Worker thread: no-op when unconfigured, _set_health_status DB mutations
- **Exception paths (17 tests):** TimeoutExpired, FileNotFoundError, OSError for ping/curl/soap; empty stdout, no newline, non-digit status codes, out-of-range codes, non-whitelisted codes, expected_string missing/found

**test_healthcheck_mc_dc.py (50 tests):**
- MC/DC for D_hc1 (health result gate) — 4 tests
- MC/DC for D_hc2 (URL sanitisation) — 5 tests
- MC/DC for D_hc3 (type auto-detection chain) — 6 tests
- MC/DC for D_hc5 (TCP host/port validation) — 8 tests
- MC/DC for D_hc7 (SOAP result gate) — 4 tests
- MC/DC for D_hc8 (RSS response gate: 5-condition curl-output chain) — 7 tests
- MC/DC for D_hc9 (RSS keyword precedence red→degraded→green) — 6 tests
- MC/DC for D_hc10 (RSS url parse guard, explicit `type: rss`) — 5 tests
- MC/DC for D_hc11 (RSS item/entry tag filter scope) — 3 tests
- Worker file lock tests (fcntl) — 2 tests

### Smoke — `tests/test_health.sh`

Quick shell-based health check: pings the root endpoint and asserts HTTP 200. Runs in under 1 second, suitable for CI pre-checks.

### Healthcheck Admin CRUD + RSS Feed — `tests/test_healthcheck_admin.py` + `tests/test_rss_feed.py` + `tests/test_rss_healthcheck.py`

- **test_healthcheck_admin.py**: Full admin CRUD for the healthchecks map — `POST /api/healthchecks` (create, all 5 types incl. `rss`), `GET /api/healthchecks` (list), `PUT /api/healthchecks/<name>` (update: partial field merge or full type migration incl. into/out of `rss`), `DELETE /api/healthchecks/<name>`; validation rejects (unknown type, missing/bad url, bad numeric fields, malformed `keywords`, duplicate name 409, 404 for missing name). Runs against a fully isolated temp app (temp DB, temp config.yaml, patched `CONFIG_PATH`/`DB_PATH`) so it never touches the live server.
- **test_rss_feed.py**: The public status feed `GET /feed.xml` — XML well-formedness, `<lastBuildDate>`/`<pubDate>` advance on status change, only status events surfaced (notes/rename filtered), `rss: {enabled: false}` → 404, admin toggle `POST /api/rss`, title/max_items clamping, empty-history feed shape.
- **test_rss_healthcheck.py**: The `rss` healthcheck type end-to-end — a real local HTTP server serves synthetic feeds; parse cases (RSS 2.0 `<item>`, Atom `<entry>` with default namespace, malformed XML → fetch failure); runtime keyword mapping (red beats degraded, case-insensitive, description+summary scanned, no-keyword feeds); feed-shape edge cases (empty feed → green, entries past the 20-entry cap not scanned, feed >512 KB → fetch failure); one-shot `run_healthchecks_once` result shape; and a full **E2E worker test** that points a live worker thread at a mutable local feed and asserts the DB item flips green→red→green with history rows recorded.

### Restart Persistence — `tests/test_restart_persistence.py`

2 critical restart-simulation tests:
- Add item via API → init_db() re-seeds → item survives
- Add then delete item → init_db() → item stays gone (no re-addition)

### Input Filter — `tests/test_input_filter.py` (100% coverage)

80+ assertions covering:
- Control character stripping (null bytes, DEL, preserves tab/newline)
- XSS detection (script tags, event handlers, javascript: protocol, encoded brackets)
- SQLi detection (compound patterns: UNION SELECT, OR 1=1, comments, stacked queries)
- Path traversal (../, %2e%2e/)
- Shell injection (backticks, $(), &&, ||, ;)
- sanitize_text: length limits, HTML escaping, non-string rejection
- validate_name: strict/relaxed charsets, length, XSS/SQLi/path/shell rejection
- validate_notes: empty allowed, XSS/SQLi rejection, max length
- validate_user_input: XSS/SQLi/path/shell in username/password
- validate_json_data: dict required, rejects list/string/number/None
- validate_int_param: int, string numeric, rejects negative/float/bool/non-numeric

---

## 2. Running Tests

### Full Run

```bash
cd ~/Developer/status-my-page

# All tests (no server needed for structural/unit)
.venv/bin/pytest tests/ -v

# Specific test categories
.venv/bin/pytest tests/test_input_filter.py -v              # Input sanitization (100%)
.venv/bin/pytest tests/test_mc_dc.py -v                      # MC/DC structural proofs (D1-D7)
.venv/bin/pytest tests/test_structural.py -v                 # Additional MC/DC (D4, D5)
.venv/bin/pytest tests/test_healthcheck.py -v                # Healthcheck functional + exception paths
.venv/bin/pytest tests/test_healthcheck_mc_dc.py -v          # Healthcheck MC/DC + worker lock
.venv/bin/pytest tests/test_history.py -v                    # Status history feature
.venv/bin/pytest tests/test_routes_and_features.py -v        # Auth, mutations, headers
.venv/bin/pytest tests/test_restart_persistence.py -v        # Restart simulation

# With coverage report
.venv/bin/pytest tests/ --cov=app --cov=healthcheck --cov=input_filter --cov-report=term-missing
```

### Individual Test Class

```bash
# Only run D5 SetNotesGuard MC/DC proofs
pytest tests/test_structural.py::Test_D5_SetNotesGuard -v

# Only the security gate on /api/delete
pytest tests/test_mc_dc.py::Test_D3_SecurityGuard::test_C1_not_admin__delete -v

# Only healthcheck exception paths
pytest tests/test_healthcheck.py::TestHealthcheckExceptionPaths -v
```

### Smoke Check

```bash
bash tests/test_health.sh                    # Default: http://localhost:8920
bash tests/test_health.sh http://myserver:9920  # Custom URL
```

---

## 3. Unit Tests (Structural / MC/DC)

### How It Works

Each structural test class targets **one compound decision** in `app.py` or `healthcheck.py`. The methodology:

1. **Baseline**: Set all conditions to pass so the function proceeds normally
2. **Individual Failure**: Flip exactly one condition to the opposite truth value while holding other constants at values that would allow the guard to pass — if the guard now blocks/skips, that condition is proven independent
3. **Independence**: If the flip alone changes the outcome (from proceed to skip or vice versa), that condition is independently proven

### Fixture Isolation

All structural tests use the `A` fixture (session-scoped) which sets up:

- A temporary SQLite database in `/tmp/tmpXXXXXX/status.db`
- Fresh `config.yaml` pointing to the temp directory
- Two seeded items: **SvcA** and **SvcB**
- Environment variable `STATUS_ADMIN_PASS_HASH=scrypt$...` for "testpass"
- Flask app with patched paths (CONFIG_PATH, DB_PATH, etc.)

The fixture ensures no state leaks between test classes. Tests within a class share the same temp DB but perform explicit cleanup (e.g., `_save_runtime()` calls clear YAML state between tests).

### Security Gate Testing Strategy (D3)

D3 is the most critical guard because it protects all mutation endpoints. It uses a **combinatorial approach** across 4 endpoints:

For each endpoint (`/api/toggle`, `/api/add`, `/api/delete`, `/api/reorder`, `/api/healthcheck/run`):
1. Baseline test: Admin + Valid CSRF + Under Rate Limit expects `200`
2. C1 failure: Non-admin request expects `403`
3. C2 failure: Invalid CSRF token expects `403`
4. C3 failure: Over rate limit (pre-seeded `_mutation_rates`) expects `403`

**Total D3 tests: 15** (1 baseline + 12 conditional failures across 5 endpoints)

The key insight is that **any bypass of ANY gate grants unauthenticated write access**, so the proof must show all three gates independently block even when the other two pass.

---

## 4. Functional Tests

### Architecture

```
pytest (client) --HTTP--> Running Flask server --SQLite--> status.db
       |                       |                          |
       |-- client.post(...)     |-- receives request         |-- executes SQL
       `-- assert response      `-- returns JSON             `-- returns row data
```

The functional tests require a running server because they use `requests.get` and `requests.post` against live HTTP endpoints, not the in-process Flask test client. This ensures CORS headers, cookie handling, and the full request pipeline are exercised.

### Test Data Lifecycle

Each functional test manages its own fixture data:
1. Create items via API (`POST /api/add`)
2. Perform mutations (toggle, rename, notes)
3. Assert database state via `SELECT` queries
4. Clean up test artifacts to avoid polluting the DB for subsequent tests

---

## 5. Healthcheck Tests

### Unit Tests (`test_healthcheck.py`)

Covers the full healthcheck subsystem:
- **Parsing & Validation**: URL scheme allowlist, host validation, numeric sanitization, SOAP auto-detection
- **Real subprocess calls**: ping, curl, soap (testing against localhost/unreachable addresses)
- **Endpoints**: GET /api/healthchecks (public), POST /api/healthcheck/run (admin+CSRF, dry-run)
- **Worker internals**: _set_health_status DB writes, history recording, no-op guards

### Exception Path Tests (17 tests in `TestHealthcheckExceptionPaths`)

Each subprocess call (`ping`, `curl`, `soap`) tested for:
- `subprocess.TimeoutExpired` → returns False/None
- `FileNotFoundError` (binary missing) → returns False/None
- `OSError` (network unreachable) → returns False/None
- **SOAP-specific**: empty stdout, missing newline, non-digit status code, code=0, code>599, non-whitelisted code, expected_string missing/found

These tests use `monkeypatch` to mock `subprocess.run` and verify graceful degradation.

### MC/DC Tests (`test_healthcheck_mc_dc.py`)

- **D_hc1** (L222): `code is not None and code in healthy_codes` — 3 tests (baseline, C1=F, C2=F)
- **D_hc2** (L124): `not url or not isinstance(url, str) or not url.strip()` — 5 tests (C1, C2, C3 each independently, baseline)
- **Worker Lock**: fcntl-based file lock proves single-worker guarantee across gunicorn processes

---

## 6. Regression Prevention

After any code change, always run:

```bash
python -m pytest tests/ -v
```

The structural suite is deliberately **brittle** because it should fail if a guard's logic changes — the condition truth table will no longer match expected outputs. This is intentional: any regression in security guards or restoration logic must surface as a test failure.

**Current coverage: 88%** (app.py 92%, healthcheck.py 76%, input_filter.py 100%)

**Test suite size: 451 tests** (as of 2026-08-17).

---

## 7. Adding New Tests

### For Structural/MC/DC Tests

1. **Identify a compound expression** in `app.py` or `healthcheck.py`: Search for lines containing `or` or `and` with 2+ conditions
2. **Extract the conditions**: Break the boolean expression into atomic predicates (C1, C2, etc.)
3. **Write baseline test**: All conditions pass so function succeeds
4. **Write individual failure tests**: For each condition Ci:
   - Set all other conditions to values that allow the guard to pass
   - Force Ci to its opposite truth value
   - Assert the guard now blocks or skips
5. **Map line refs**: Record the source line numbers where each condition lives in app.py

### For Functional Tests

1. Add a `test_*` method to `tests/test_history.py` or `tests/test_routes_and_features.py`
2. Use `requests.Session()` or `requests.post()` with a running server URL
3. Seed any required test data before assertions
4. Verify HTTP status code AND response JSON body
5. Clean up test artifacts after assertions

### For Healthcheck Tests

1. Add to `tests/test_healthcheck.py` for functional tests
2. Add to `tests/test_healthcheck_mc_dc.py` for MC/DC proofs
3. Use `monkeypatch.setattr(subprocess, "run", mock_fn)` for exception-path tests
4. Mock `fcntl.flock` behavior for worker lock tests

---

*Document version: 2.1 | Last updated: 2026-08-17 | Author: Simar Sahni*
