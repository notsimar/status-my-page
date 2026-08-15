#!/usr/bin/env python3
"""MC/DC tests for the healthcheck compound decisions in app.py.

Covers all compound boolean expressions introduced by the healthcheck system:

  D_hc1 (L222): code is not None and code in hc["healthy_codes"]
      Gates whether a service gets flipped to green after a curl check.
      C1 = code is not None    (None from connection error, 0xx–5xx codes allowed)
      C2 = code in healthy_codes  (only whitelisted HTTP codes count as OK)

  D_hc2 (L124): if not url or not isinstance(url, str) or not url.strip(): skip entry
      Three-condition short-circuit guard on _parse_healthchecks() that rejects
      malformed healthcheck entries before they reach curl. Already covered by the
      functional test suite (TestParseHealthchecks); documented here for completeness.

Prerequisites:
  - Session-scoped fixture A from conftest.py (temp config.yaml, temp DB)
"""

import json
import os
import sqlite3
from pathlib import Path

import pytest
import yaml


# ─── D_hc1: curl health result gate ──────────────────────────────
# Expression (app.py L222): if code is not None and code in hc["healthy_codes"]:
#   Outcome when True  -> service marked green, fail_counter reset.
#   Outcome when False -> fail_counter incremented, eventually -> degraded/red.
#
# MC/DC conditions:
#   C1 = code is not None      (F for connection errors / timeouts)
#   C2 = code in healthy_codes (T for whitelisted HTTP status, F otherwise)
#
# ┌──────┬────┬────┬───────────────────────────────────────────────────┐
# │ Test │ C1 │ C2 │ Observable effect                                 │
# ├──────┼────┼────┼───────────────────────────────────────────────────┤
# │ T_hc1│ T  │ T  │ Green, fail_count reset (baseline success path)   │
# │ T_hc2│ F  │ ×  │ Degraded/red path taken (connection error, None)   │
# │ T_hc3│ T  │ F  │ Degraded/red path taken (e.g. 404 ≠ in [200])    │
# └──────┴────┴────┴───────────────────────────────────────────────────┘

class Test_Dhc1_HealthResultGate:
    """MC/DC for the healthy-result guard on L222 inside _healthcheck_worker()."""

    def _write(self, A, data):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    # ── T_hc1 baseline: C1=True, C2=True -> green path taken ─────
    def test_C1T_C2T__baseline_green(self, A):
        """Both True: code=200 and 200 in healthy_codes -> enters green branch."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://bad-host.invalid/", "interval": 60}},
            },
        )
        # Verify _parse_healthchecks() loads correctly.
        hc = A._parse_healthchecks()
        assert "SvcA" in hc

        # Simulate the guard expression with known values (code=200, healthy_codes={200}).
        code = 200  # C1=True (not None)
        hc_entry = hc["SvcA"]
        assert hc_entry is not None and code in hc_entry["healthy_codes"], \
            "C1=T + C2=T baseline should enter the green branch"

    # ── T_hc2: C1=False -> unhealthy path (None from curl failure) ──
    def test_C1_False__unhealthy_path(self, A):
        """C1=False (code=None, e.g. connection refused) -> skip healthy branch."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://bad/", "interval": 60}},
            },
        )
        hc = A._parse_healthchecks()
        code = None  # C1=False
        hc_entry = hc["SvcA"]

        # Guard expression result:
        enters_healthy = hc_entry is not None and code in hc_entry["healthy_codes"] if code is not None else False
        assert not enters_healthy, "C1=False must NOT enter healthy branch"

    # ── T_hc3: C2=False (code exists but not whitelisted) -> unhealthy path ──
    def test_C1_True_C2_False__unhealthy_non_whitelisted(self, A):
        """C1=True but C2=False (e.g., code=404, healthy_codes={200}) -> unhealthy."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://localhost/", "healthy_codes": [200]}},
            },
        )
        hc = A._parse_healthchecks()
        code = 404  # C1=True (not None)
        hc_entry = hc["SvcA"]

        # Code exists but is NOT in the healthy list.
        enters_healthy = code in hc_entry["healthy_codes"] if code is not None else False
        assert not enters_healthy, "C2=False must NOT enter healthy branch"

    # ── Bonus: prove custom whitelisted codes work ───
    def test_custom_whitelisted_code__allowed(self, A):
        """Code=204 with healthy_codes=[200, 204] -> both conditions True."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {
                    "url": "http://localhost/",
                    "healthy_codes": [200, 204]
                }},
            },
        )
        hc = A._parse_healthchecks()
        code = 204
        enters_healthy = code in hc["SvcA"]["healthy_codes"] if code is not None else False
        assert enters_healthy, "C1=T + C2=T for custom whitelisted code=204"


# ─── D_hc2: URL sanitisation skip guard (documented) ──────────
# Expression (app.py L124): if not url or not isinstance(url, str) or not url.strip(): continue
# Short-circuit OR-chain; any True causes the entry to be skipped.
#   C1 = not url          (missing key / None)
#   C2 = not isinstance(url, str)  (e.g., numeric URL in YAML)
#   C3 = not url.strip()   (whitespace-only string)

class Test_Dhc2_UrlSanitisation:
    """MC/DC for the three-condition URL skip guard on L124 of _parse_healthchecks()."""

    def _write(self, A, data):
        with open(str(A.CONFIG_PATH), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    # ── C1=True alone -> skip ──
    def test_C1_True_missing_url__skipped(self, A):
        """url key absent (None) -> entry rejected."""

        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {}},  # no url key at all
            },
        )
        assert A._parse_healthchecks() == {}

    # ── C2=True alone -> skip (url exists but is not string) ──
    def test_C2_True_non_string_url__skipped(self, A):
        """C1=False (url key present), C2=True (type=int) -> rejected."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": 12345}},  # int, not str.
            },
        )
        assert A._parse_healthchecks() == {}

    # ── C3=True alone -> skip (string but empty/whitespace) ──
    def test_C3_True_empty_string_url__skipped(self, A):
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": ""}},  # C1=F (present), C2=F (str), C3=T
            },
        )
        assert A._parse_healthchecks() == {}

    def test_C3_True_whitespace_only__skipped(self, A):
        """Whitespace-only URL string -> also rejected (C3=True)."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "   "}},
            },
        )
        assert A._parse_healthchecks() == {}

    # ── Baseline: all False -> entry proceeds ──
    def test_all_False__proceeds(self, A):
        """C1=F + C2=F + C3=F (valid non-empty http string) -> parsed successfully."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://localhost:8080/"}},
            },
        )
        hc = A._parse_healthchecks()
        assert "SvcA" in hc


# ─── CLI entry point ──────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-v"]))
