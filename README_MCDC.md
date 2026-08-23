# Structural Testing Documentation: MC/DC Coverage

This document outlines the **Modified Condition/Decision Coverage (MC/DC)** strategy applied to `status-my-page`. 

## Overview

While behavioral testing (TDD) ensures that features work as expected, structural testing proves that the internal logic—specifically compound boolean expressions—is correct and contains no redundant or dead conditions.

We use MC/DC to verify "Guard" logic where multiple conditions determine whether an action is permitted or skipped.

## Decision Mapping & Proof Table

The following table maps every critical compound decision in the codebase (formerly the `app.py` monolith — now split across `statuspage/db.py`, `statuspage/services.py`, and `statuspage/auth.py`) to its corresponding test cases. To satisfy MC/DC, we must prove that each condition $\text{C}_n$ can independently change the outcome of the decision.

### D1: DB Status & State Persistence Across Restart
**Behavior:** Verifies that items and their status in SQLite are maintained across `init_db()` restarts without relying on YAML runtime overrides.

---

### D2: Notes Persistence Across Restart
**Behavior:** Verifies that note text stored in SQLite persists across `init_db()` restarts.

---

### D3: Global API Security Guard (`statuspage/auth.py` — `require_admin`)
**Expression:** `if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip): abort(403)`

| Test Method | C1 (Admin) | C2 (CSRF OK) | C3 (Rate OK) | Outcome | Proof |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `test_baseline_all_ok__success` | $\text{T}$ | $\text{T}$ | $\text{T}$ | **200 OK** | Baseline (Success) |
| `test_C1_not_admin__403` | $\text{F}$ | $\text{X}$ | $\text{X}$ | **403** | $\text{C}_1$ independently gates |
| `test_C2_bad_csrf__403` | $\text{T}$ | $\text{F}$ | $\text{T}$ | **403** | $\text{C}_2$ independently gates |
| `test_C3_rate_limited__403` | $\\text{T}$ | $\\text{T}$ | $\\text{F}$ | **403** | $\\text{C}_3$ independently gates |

---

### D4: Reorder API Position Updates
**Behavior:** Verifies that the `/api/reorder` endpoint updates service positions directly in SQLite.

---

### D5: Set Notes DB Update
**Behavior:** Verifies that `/api/notes/<id>` updates notes directly in SQLite.

---

## How to Run Tests

MC/DC structural tests are located in three files:

```bash
# statuspage decisions (D1, D2, D3, D6, D7)
./.venv/bin/pytest tests/test_mc_dc.py -v

# statuspage decisions (D4, D5)
./.venv/bin/pytest tests/test_structural.py -v

# Healthcheck worker decisions (D_hc1–D_hc11) + worker lock
./.venv/bin/pytest tests/test_healthcheck_mc_dc.py -v

# All structural tests together
./.venv/bin/pytest tests/test_mc_dc.py tests/test_structural.py \
    tests/test_healthcheck_mc_dc.py -v
```

## Implementation Notes
- **Isolation**: Session/function-scoped fixtures repoint `CONFIG_PATH` and `DB_PATH` to temporary directories, preventing tests from mutating the real environment.
- **State Mutation**: For rate-limit testing ($\text{C}_3$), we directly mutate `_mutation_rates` to simulate a high-traffic IP without 60+ real requests.
- **Contexts**: All DB calls wrapped in `app.test_request_context()` for Flask's `g` object.
- **Healthcheck decisions**: `_run_rss_feed_check` and its siblings are pure over the curl output string, so `subprocess.run` is monkeypatched to a fixed stdout — each condition is flipped in exactly one test with no network access.

## Healthcheck Decision Proofs (D_hc1–D_hc11)

Proven in `tests/test_healthcheck_mc_dc.py` against the pure parsing/dispatch functions (no network, no DB — `subprocess.run` and config are monkeypatched per test):

| Decision | Location | Expression | Tests |
|------|----------|------------|-------|
| D_hc1: HealthResultGate | `_impl._run_curl_check` | `code in healthy_codes and (not expected or expected in body)` | 4 |
| D_hc2: UrlSanitisation | `_impl._parse_healthchecks` (curl) | `not url or not isinstance(url, str) or not url.strip() or not _safe_url(...)` | 5 |
| D_hc3: TypeAutoDetection | `_impl._parse_healthchecks` | soap → tcp → ping → curl elif chain | 6 |
| D_hc5: TcpValidation | `_impl._parse_healthchecks` (tcp) | `not host or not isinstance(host, str) or not _safe_host(host) or port not 1..65535` | 8 |
| D_hc7: SoapValidation | `_impl._parse_healthchecks` (soap) | `not url or not isinstance(url, str) or not url.strip() or not _safe_url(url.strip())` | 4 |
| D_hc8: RssResponseGate | `_impl._run_rss_feed_check` | `"\n" in stdout and code.isdigit() and 1≤code≤599 and code==200 and no ET.ParseError` | 7 |
| D_hc9: RssKeywordPrecedence | `_impl._run_rss_feed_check` | `red set & match → red; degraded set & match → degraded; else green` (red precedence) | 6 |
| D_hc10: RssUrlGuard | `_impl._parse_healthchecks` (rss) | `not url or not isinstance(url, str) or not url.strip() or not _safe_url(...)` | 5 |
| D_hc11: RssEntryFilter | `_impl._run_rss_feed_check` | tag in `item`/`entry` **and** local-name in `title`/`description`/`summary` | 3 |

Plus 2 worker single-instance lock tests (fcntl `LOCK_EX | LOCK_NB`).

## Structural Coverage Summary

| Decision | Test File | Conditions | Tests | Status |
|----------|-----------|------------|-------|--------|
| D1: RestoreStatus | test_mc_dc.py | 2 (C1,C2) | 4 tests | ✅ Complete |
| D2: NotesRestore | test_mc_dc.py | 2 (C1,C2) | 3 tests | ✅ Complete |
| D3: SecurityGate | test_mc_dc.py | 3 (C1,C2,C3) | 16 tests across 4 endpoints | ✅ Complete |
| D4: ReorderOverride | test_structural.py | 2 (C1,C2) | 7 tests | ✅ Complete |
| D5: SetNotesGuard | test_structural.py | 2 (C1,C2) | 7 tests | ✅ Complete |
| D6: CsrfInternalGuard | test_mc_dc.py | 2 (C1,C2) | 4 tests | ✅ Complete |
| D7: DeleteCleanupGate | test_mc_dc.py | 2 (C1,C2) | 5 tests | ✅ Complete |
| D_hc1–D_hc11 | test_healthcheck_mc_dc.py | 2–5 each | 48 tests | ✅ Complete |

**Total: 18 compound decisions with complete MC/DC proofs (100%) — 73 structural MC/DC tests.**
