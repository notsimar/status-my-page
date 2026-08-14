# Testing & Quality Assurance Guide

## Table of Contents

- [1. Test Categories](#1-test-categories)
- [2. Running Tests](#2-running-tests)
- [3. Unit Tests (Structural / MC/DC)](#3-unit-tests-structural--mcdc)
- [4. Functional Tests](#4-functional-tests)
- [5. Regression & Integration](#5-regression--integration)
- [6. Adding New Tests](#6-adding-new-tests)

---

## 1. Test Categories

This project organizes tests into three categories:

### Unit (Structural / MC/DC) — `tests/test_structural.py` + `tests/test_mc_dc.py`

Prove that every guard condition in compound boolean expressions **independently** controls the outcome. These are not "happy-path" tests — they are designed to fail at a specific gate.

| Decision | Location | Expression | Test File | Coverage |
|----------|----------|------------|-----------|----------|
| D1: RestoreStatus | app.py L342 | `not in seed_set or state in ('green','')` | Test_D1_RestoreStatus (4 tests) | Full MC/DC |
| D2: RestoreNotes | app.py L355 | `not in seed_set or not note_text.strip()` | Test_D2_NotesRestore (3 tests) | Full MC/DC |
| D3: SecurityGate | app.py L680+ | `_not_admin() or not _check_csrf() or not _check_mutation_rate(ip)` | Test_D3_SecurityGuard (14 tests across 4 endpoints) | Full MC/DC |
| D4: ReorderOverride | app.py L395 | `reorder_list and isinstance(reorder_list, list)` | Test_D4_ReorderOverride (7 tests) | Full MC/DC |
| D5: SetNotesGuard | app.py L547 | `current_row and notes.strip()` | Test_D5_SetNotesGuard (7 tests) | Full MC/DC |

**Total: 35 structural tests, 100% decision coverage, all conditions MC/DC-proven.**

### Functional — `tests/test_history.py`

End-to-end HTTP tests that verify the API behavior against a running server with a seeded database.

- **test_status_toggle**: Cycles status and records history entry
- **test_notes_update**: Writes notes and verifies persistence
- **test_history_newest_first**: Fetches history timeline, verifies reverse chronological order
- **test_fields_present**: Validates each history record has event_type, item_id, old_value, new_value, occurred
- **test_public_api_access**: Confirms /api/history/<id> is publicly readable
- **test_api_errors**: Verifies proper HTTP status codes for bad requests

### Smoke — `tests/test_health.sh`

Quick shell-based health check: pings the root endpoint and asserts HTTP 200. Runs in under 1 second, suitable for CI pre-checks.

---

## 2. Running Tests

### Full Run

```bash
cd ~/Developer/status-my-page

# Start server first (in a separate terminal):
source .venv/bin/activate
./start.sh

# Then run all tests:
python -m pytest tests/ -v

# Or just structural:
python -m pytest tests/test_structural.py tests/test_mc_dc.py -v

# Or just functional:
python -m pytest tests/test_history.py -v
```

### Individual Test Class

```bash
# Only run D5 SetNotesGuard MC/DC proofs
pytest tests/test_structural.py::Test_D5_SetNotesGuard -v

# Only the security gate on /api/delete
pytest tests/test_mc_dc.py::Test_D3_SecurityGuard::test_C1_not_admin__delete -v
```

### Without pytest (Functional Only)

```bash
python tests/test_history.py
```

### Smoke Check

```bash
bash tests/test_health.sh
```

---

## 3. Unit Tests (Structural / MC/DC)

### How It Works

Each structural test class targets **one compound decision** in `app.py`. The methodology:

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

For each endpoint (`/api/toggle`, `/api/add`, `/api/delete`, `/api/reorder`):
1. Baseline test: Admin + Valid CSRF + Under Rate Limit expects `200`
2. C1 failure: Non-admin request expects `403`
3. C2 failure: Invalid CSRF token expects `403`
4. C3 failure: Over rate limit (pre-seeded `_mutation_rates`) expects `403`

**Total D3 tests: 15** (1 baseline + 12 conditional failures across 4 endpoints)

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

## 5. Adding New Tests

### For Structural/MC/DC Tests

1. **Identify a compound expression** in `app.py`: Search for lines containing `or` or `and` with 2+ conditions
2. **Extract the conditions**: Break the boolean expression into atomic predicates (C1, C2, etc.)
3. **Write baseline test**: All conditions pass so function succeeds
4. **Write individual failure tests**: For each condition Ci:
   - Set all other conditions to values that allow the guard to pass
   - Force Ci to its opposite truth value
   - Assert the guard now blocks or skips
5. **Map line refs**: Record the source line numbers where each condition lives in app.py

### For Functional Tests

1. Add a `test_*` method to `tests/test_history.py`
2. Use `requests.Session()` or `requests.post()` with a running server URL
3. Seed any required test data before assertions
4. Verify HTTP status code AND response JSON body
5. Clean up test artifacts after assertions

---

## 6. Regression Prevention

After any code change, always run:

```bash
python -m pytest tests/ -v
```

The structural suite is deliberately **brittle** because it should fail if a guard's logic changes — the condition truth table will no longer match expected outputs. This is intentional: any regression in security guards or restoration logic must surface as a test failure.

---

*Document version: 1.0 | Last updated: 2026-08-13 | Author: Simar Sahni*
