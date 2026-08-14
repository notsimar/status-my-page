# Structural Testing Documentation: MC/DC Coverage

This document outlines the **Modified Condition/Decision Coverage (MC/DC)** strategy applied to `status-my-page`. 

## Overview

While behavioral testing (TDD) ensures that features work as expected, structural testing proves that the internal logic—specifically compound boolean expressions—is correct and contains no redundant or dead conditions.

We use MC/DC to verify "Guard" logic where multiple conditions determine whether an action is permitted or skipped.

## Decision Mapping & Proof Table

The following table maps every critical compound decision in `app.py` to its corresponding test cases. To satisfy MC/DC, we must prove that each condition $\text{C}_n$ can independently change the outcome of the decision.

### D1: Runtime Status Restore (app.py L342)
**Expression:** `if item_name not in seed_set or new_state in ('green', ''): continue`

| Test Method | C1 (In Seed) | C2 (Valid State) | Outcome | Proof 
| :--- | :---: | :---: | :---: | :--- |
| `test_C1_false_C2_false__restores_degraded` | $\text{T}$ | $\text{F}$ | **Restore** | Baseline (Success) |
| `test_C1_true_unknown_item__skipped` | $\text{F}$ | $\text{X}$ | **Skip** | $\text{C}_1$ independently gates |
| `test_C2_true_green_state__skipped` | $\text{T}$ | $\text{T}$ | **Skip** | $\text{C}_2$ independently gates |

---

### D2: Runtime Note Restore (L354)
**Expression:** `if item_name not in seed_set or not note_text.strip(): continue`

| Test Method | C1 (In Seed) | C2 (Text Set) | Outcome | Proof |
| :--- | :---: | :---: | :---: | :--- |
| `test_C1_false_C2_false__restores_notes` | $\text{T}$ | $\text{T}$ | **Restore** | Baseline (Success) |
| `test_C1_true_unknown_item__skipped` | $\text{F}$ | $\text{X}$ | **Skip** | $\text{C}_1$ independently gates |
| `test_C2_true_empty_note__skipped` | $\text{T}$ | $\text{F}$ | **Skip** | $\text{C}_2$ independently gates |

---

### D3: Global API Security Guard (L679, L689, L701)
**Expression:** `if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip): abort(403)`

| Test Method | C1 (Admin) | C2 (CSRF OK) | C3 (Rate OK) | Outcome | Proof |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `test_baseline_all_ok__success` | $\text{T}$ | $\text{T}$ | $\text{T}$ | **200 OK** | Baseline (Success) |
| `test_C1_not_admin__403` | $\text{F}$ | $\text{X}$ | $\text{X}$ | **403** | $\text{C}_1$ independently gates |
| `test_C2_bad_csrf__403` | $\text{T}$ | $\text{F}$ | $\text{T}$ | **403** | $\text{C}_2$ independently gates |
| `test_C3_rate_limited__403` | $\\text{T}$ | $\\text{T}$ | $\\text{F}$ | **403** | $\\text{C}_3$ independently gates |

---

### D4: Reorder Override Guard (app.py L395)
**Expression:** `if reorder_list and isinstance(reorder_list, list):`

| Test Method | C1 (Truthy) | C2 (Is List) | Outcome | Proof |
| :--- | :---: | :---: | :---: | :--- |
| `test_C1_none__skipped` | $\\text{F}$ | — | **Skipped** | $\\text{C}_1$ independently gates (falsy) |
| `test_C1_empty_list__skipped` | $\\text{F}$ | — | **Skipped** | Empty list is also falsy |
| `test_C2_string_type__skipped` | $\\text{T}$ | $\\text{F}$ | **Skipped** | Wrong type skips block |
| `test_C2_int_type__skipped` | $\\text{T}$ | $\\text{F}$ | **Skipped** | C2 independently gates (bad type) |
| `test_C2_dict_type__skipped` | $\\text{T}$ | $\\text{F}$ | **Skipped** | Dict type also rejected |
| `test_C1_True_C2_True__baseline_reordered` | $\\text{T}$ | $\\text{T}$ | **Reordered** | Baseline (Success) |

---

### D5: Set Notes YAML-Persist Guard (app.py L547)
**Expression:** `if current_row and notes.strip(): ... _save_runtime()`

Both conditions must be True for notes to persist to `_runtime.notes`. set_notes() always returns HTTP 200 regardless of C1/C2 — MC/DC tests verify YAML state, not HTTP status.

| Test Method | C1 (Row Exists) | C2 (Text Stripped) | Outcome | Proof |
| :--- | :---: | :---: | :---: | :--- |
| `test_C1_row_missing__yaml_skipped` | $\\text{F}$ | — | **YAML Skip** | $\\text{C}_1$ independently gates (missing item) |
| `test_C1_negative_id__yaml_skipped` | $\\text{F}$ | — | **YAML Skip** | Negative ID is also missing row |
| `test_C2_empty_text__yaml_skipped` | $\\text{T}$ | $\\text{F}$ | **YAML Skip** | $\\text{C}_2$ independently gates (empty string) |
| `test_C2_whitespace_only__yaml_skipped` | $\\text{T}$ | $\\text{F}$ | **YAML Skip** | Whitespace-only is falsy after strip() |
| `test_C2_tab_chars_only__yaml_skipped` | $\\text{T}$ | $\\text{F}$ | **YAML Skip** | Tab chars only also fall to C2=F |
| `test_C1_True_C2_True__baseline_note_saved` | $\\text{T}$ | $\\text{T}$ | **Note Saved** | Baseline (Success) |
| `test_C1_True_C2_False_with_existing_note__not_overwritten` | $\\text{T}$ | $\\text{F}$ | **Unchanged** | C2=False idempotent skip preserves old value |

---

## How to Run Tests

MC/DC structural tests for D1–D5 are located in two files:

```bash
# Original test file (D1, D2, D3)
./.venv/bin/pytest tests/test_mc_dc.py -v

# Extended test file (D4, D5) — NEW!
./.venv/bin/pytest tests/test_structural.py -v

# All structural tests together
./.venv/bin/pytest tests/test_mc_dc.py tests/test_structural.py -v
```

## Implementation Notes
- **Isolation**: Session-scoped fixture repoints `CONFIG_PATH` and `DB_PATH` to temporary directories, preventing tests from mutating the real environment.
- **State Mutation**: For rate-limit testing ($\\text{C}_3$), we directly mutate `_mutation_rates` to simulate high-traffic IP without 60+ real requests.
- **Contexts**: All DB calls wrapped in `app.test_request_context()` for Flask's `g` object.
- **D5 YAML Verification**: set_notes() always returns HTTP 200; MC/DC proofs verify `_runtime.notes` state via `$ yaml.safe_load(open(CONFIG_PATH))`, not HTTP status codes.

## Structural Coverage Summary

| Decision | Line(s) | Conditions | Tests | Status |
|----------|---------|------------|-------|--------|
| D1: RestoreStatus | L342 | 2 (C1,C2) | 4 tests | ✅ Complete |
| D2: NotesRestore | L354 | 2 (C1,C2) | 3 tests | ✅ Complete |
| D3: SecurityGate | L679,689,701,743,766,786 | 3 (C1,C2,C3) | 15 tests across 4 endpoints | ✅ Complete |
| D4: ReorderOverride | L395 | 2 (C1,C2) | 7 tests | ✅ Complete **[NEW]** |
| D5: SetNotesGuard | L547 | 2 (C1,C2) | 7 tests | ✅ Complete **[NEW]** |

**Total structural coverage: 5/5 decisions with complete MC/DC proofs (100%).**
