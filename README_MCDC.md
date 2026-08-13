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
| `test_C3_rate_limited__403` | $\text{T}$ | $\text{T}$ | $\text{F}$ | **403** | $\text{C}_3$ independently gates |

## How to Run Tests

Structural tests are located in `tests/test_mc_dc.py`. They require the virtual environment's pytest:

```bash
./.venv/bin/pytest tests/test_mc_dc.py -v
```

## Implementation Notes
- **Isolation**: We use a session-scoped fixture to repoint `CONFIG_PATH` and `DB_PATH` to temporary directories, preventing tests from mutating the real environment.
- **State Mutation**: For rate-limit testing ($\text{C}_3$), we directly mutate the internal `_mutation_rates` dictionary of the app module to simulate a high-traffic IP without making 60+ real requests.
- **Contexts**: All DB calls are wrapped in `app.test_request_context()` to support Flask's `g` object.
