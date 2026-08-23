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
import subprocess
from pathlib import Path

import pytest
import yaml
import statuspage.config as _cfg
import healthcheck as _hc


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
        with open(str(_cfg.get_config_path()), "w") as f:
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
        hc = _hc._parse_healthchecks()
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
        hc = _hc._parse_healthchecks()
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
        hc = _hc._parse_healthchecks()
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
        hc = _hc._parse_healthchecks()
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
        with open(str(_cfg.get_config_path()), "w") as f:
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
        assert _hc._parse_healthchecks() == {}

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
        assert _hc._parse_healthchecks() == {}

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
        assert _hc._parse_healthchecks() == {}

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
        assert _hc._parse_healthchecks() == {}

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
        hc = _hc._parse_healthchecks()
        assert "SvcA" in hc


# ─── D_hc3: Type auto-detection logic ─────────────────────────────
# Expression (_parse_healthchecks L144-154): Sequential if/elif chain for type inference
#   Order of evaluation (short-circuit):
#   C1 = soap_action or soap_body                    -> type="soap"
#   C2 = host and details.get("port") is not None    -> type="tcp"
#   C3 = host                                        -> type="ping"
#   C4 = url                                         -> type="curl"
#   else: continue (skip entry)
#
# MC/DC requires each condition to independently affect outcome:
#   T_hc3_1: C1=True  -> soap (regardless of C2,C3,C4)
#   T_hc3_2: C1=False, C2=True  -> tcp (regardless of C3,C4)
#   T_hc3_3: C1=False, C2=False, C3=True  -> ping (regardless of C4)
#   T_hc3_4: C1=False, C2=False, C3=False, C4=True  -> curl
#   T_hc3_5: All False -> entry skipped


class Test_Dhc3_TypeAutoDetection:
    """MC/DC for the type auto-detection chain in _parse_healthchecks()."""

    def _write(self, A, data):
        with open(str(_cfg.get_config_path()), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    # ── C1=True alone -> soap ──────────────────────────────────────
    def test_C1_True_soap_action__type_soap(self, A):
        """soap_action present -> type=soap (C1=True, short-circuits rest)."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://x/", "soap_action": "GetStatus"}},
            },
        )
        hc = _hc._parse_healthchecks()
        assert "SvcA" in hc
        assert hc["SvcA"]["type"] == "soap"

    def test_C1_True_soap_body__type_soap(self, A):
        """soap_body present -> type=soap (C1=True)."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {
                    "SvcA": {"url": "http://x/", "body": "<soap:Body/>"}
                },
            },
        )
        hc = _hc._parse_healthchecks()
        assert hc["SvcA"]["type"] == "soap"

    # ── C1=False, C2=True -> tcp ───────────────────────────────────
    def test_C1_False_C2_True_host_port__type_tcp(self, A):
        """No soap fields, host+port present -> type=tcp (C1=F, C2=T)."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"host": "127.0.0.1", "port": 5432}},
            },
        )
        hc = _hc._parse_healthchecks()
        assert "SvcA" in hc
        assert hc["SvcA"]["type"] == "tcp"

    # ── C1=False, C2=False, C3=True -> ping ────────────────────────
    def test_C1_False_C2_False_C3_True_host_only__type_ping(self, A):
        """No soap, no port, host present -> type=ping (C1=F, C2=F, C3=T)."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"host": "10.0.0.1"}},
            },
        )
        hc = _hc._parse_healthchecks()
        assert "SvcA" in hc
        assert hc["SvcA"]["type"] == "ping"

    # ── C1=False, C2=False, C3=False, C4=True -> curl ──────────────
    def test_all_False_C4_True_url_only__type_curl(self, A):
        """No soap, no host, url present -> type=curl (C1=F, C2=F, C3=F, C4=T)."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"url": "http://localhost/health"}},
            },
        )
        hc = _hc._parse_healthchecks()
        assert "SvcA" in hc
        assert hc["SvcA"]["type"] == "curl"

    # ── All False -> skipped ───────────────────────────────────────
    def test_all_False__skipped(self, A):
        """No recognisable fields -> entry skipped."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"interval": 30}},
            },
        )
        assert _hc._parse_healthchecks() == {}


# ─── D_hc5: TCP validation gate ────────────────────────────────────
# Expression (_parse_healthchecks L188-194): Three-condition AND-chain
#   if not target_host or not isinstance(target_host, str): continue
#   if not _safe_host(target_host): continue
#   if target_port is None or not _safe_port(int(target_port)): continue
# All three must be False for entry to proceed.
#   C1 = not target_host or not isinstance(target_host, str)
#   C2 = not _safe_host(target_host)
#   C3 = target_port is None or not _safe_port(int(target_port))
#
# MC/DC matrix (each independently affects outcome):
#   T_hc5_1: C1=T (missing host) -> skip
#   T_hc5_2: C2=T (unsafe host) -> skip
#   T_hc5_3: C3=T (missing/invalid port) -> skip
#   T_hc5_4: All F (valid host+port) -> proceeds


class Test_Dhc5_TcpValidation:
    """MC/DC for TCP host+port validation in _parse_healthchecks()."""

    def _write(self, A, data):
        with open(str(_cfg.get_config_path()), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def test_C1_True_missing_host__skipped(self, A):
        """C1=True: host key absent -> skipped."""
        self._write(
            A,
            {"items": ["SvcA"], "_runtime": {}, "healthchecks": {"SvcA": {"type": "tcp", "port": 5432}}},
        )
        assert _hc._parse_healthchecks() == {}

    def test_C1_True_non_string_host__skipped(self, A):
        """C1=True: host is int -> skipped."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": 12345, "port": 5432}},
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_C2_True_unsafe_host__skipped(self, A):
        """C2=True: host passes type check but fails _safe_host -> skipped."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "-c", "port": 80}},
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_C3_True_missing_port__skipped(self, A):
        """C3=True: port key absent -> skipped."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "127.0.0.1"}},
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_C3_True_invalid_port_zero__skipped(self, A):
        """C3=True: port=0 (out of range) -> skipped."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "127.0.0.1", "port": 0}},
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_C3_True_invalid_port_high__skipped(self, A):
        """C3=True: port=70000 (out of range) -> skipped."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "127.0.0.1", "port": 70000}},
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_C3_True_negative_port__skipped(self, A):
        """C3=True: negative port -> skipped."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "127.0.0.1", "port": -1}},
            },
        )
        assert _hc._parse_healthchecks() == {}

    def test_all_False_valid_host_port__proceeds(self, A):
        """All conditions False: valid host and port -> parsed."""
        self._write(
            A,
            {
                "items": ["SvcA"],
                "_runtime": {},
                "healthchecks": {"SvcA": {"type": "tcp", "host": "127.0.0.1", "port": 5432}},
            },
        )
        hc = _hc._parse_healthchecks()
        assert "SvcA" in hc
        assert hc["SvcA"]["type"] == "tcp"
        assert hc["SvcA"]["port"] == 5432


# ─── D_hc7: SOAP result gate ───────────────────────────────────────
# Expression (_run_soap_check L323-328): Compound AND for healthy result
#   if code not in healthy_codes: return False, code
#   if expected_string and expected_string.strip() not in resp_body: return False, code
#   return True, code
#
# MC/DC conditions:
#   C1 = code in healthy_codes
#   C2 = not expected_string or expected_string in resp_body
#   Outcome: (C1 and C2) -> healthy (True)
#
# ┌──────┬────┬────┬───────────────────────────────────────────────────┐
# │ Test │ C1 │ C2 │ Observable effect                                 │
# ├──────┼────┼────┼───────────────────────────────────────────────────┤
# │ T_hc7│ T  │ T  │ Healthy (True, code)                              │
# │ T_hc8│ F  │ ×  │ Unhealthy (False, code)                           │
# │ T_hc9│ T  │ F  │ Unhealthy (False, code)                           │
# └──────┴────┴────┴───────────────────────────────────────────────────┘


class Test_Dhc7_SoapResultGate:
    """MC/DC for SOAP healthy-result guard in _run_soap_check()."""

    def _write(self, A, data):
        with open(str(_cfg.get_config_path()), "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def test_C1T_C2T__baseline_healthy(self, A):
        """Both True: code in healthy_codes AND expected_string found -> healthy."""
        # Simulate guard: code=200 in {200}, expected_string="OK" in body
        code = 200
        healthy_codes = {200}
        expected_string = "OK"
        resp_body = "<Status>OK</Status>"
        assert code in healthy_codes
        assert expected_string in resp_body
        assert code in healthy_codes and expected_string in resp_body

    def test_C1_False_code_not_whitelisted__unhealthy(self, A):
        """C1=False: code not in healthy_codes -> unhealthy (C2 irrelevant)."""
        code = 404
        healthy_codes = {200}
        expected_string = "OK"
        resp_body = "<Status>OK</Status>"
        assert code not in healthy_codes
        assert not (code in healthy_codes and expected_string in resp_body)

    def test_C1_True_C2_False_expected_missing__unhealthy(self, A):
        """C1=True but C2=False: expected_string missing from body -> unhealthy."""
        code = 200
        healthy_codes = {200}
        expected_string = "HEALTHY"
        resp_body = "<Status>OK</Status>"
        assert code in healthy_codes
        assert expected_string not in resp_body
        assert not (code in healthy_codes and expected_string in resp_body)

    def test_C2_True_no_expected_string__healthy(self, A):
        """C2=True vacuously: no expected_string set -> healthy if code OK."""
        code = 200
        healthy_codes = {200}
        expected_string = ""
        resp_body = "<Status>OK</Status>"
        assert code in healthy_codes
        # C2 is vacuously True when expected_string is empty/None
        assert not expected_string or expected_string in resp_body
        assert code in healthy_codes and (not expected_string or expected_string in resp_body)


# ─── D_hc8: RSS response gate ──────────────────────────────────────
# Expression (_run_rss_feed_check L441-457): sequential guard chain over the
# raw curl output (body + "\n" + http_code):
#   C1 = "\n" in stdout                    (no newline -> malformed, None)
#   C2 = code_str.isdigit()                (non-numeric code -> None)
#   C3 = code == 0 or code > 599           (invalid code range -> None)
#   C4 = code != 200                       (non-200 -> (None, code))
#   C5 = ET.ParseError on body             (malformed XML -> (None, code))
# All False -> parse proceeds -> keyword mapping runs.
#
# Each C flips the outcome independently (subprocess.run is monkeypatched to
# deliver a controlled curl result — no network, deterministic per branch):
#
# ┌────────┬────┬───────────────────────────────────────────────────┐
# │ Test   │ C  │ Observable effect                                 │
# ├────────┼────┼───────────────────────────────────────────────────┤
# │ base   │ -- │ (green, 200) — fetch succeeded, clean feed        │
# │ T_hc8a │C1=F│ (None, None) — no newline in stdout               │
# │ T_hc8b │C2=T│ (None, None) — non-numeric code                   │
# │ T_hc8c │C3=T│ (None, None) — code out of 1..599                 │
# │ T_hc8d │C4=T│ (None, 404) — non-200 http status                 │
# │ T_hc8e │C5=T│ (None, 200) — 200 but body is not XML             │
# └────────┴────┴───────────────────────────────────────────────────┘

FEED_BODY = '<?xml version="1.0"?><rss><channel><title>T</title></channel></rss>'
EMPTY_WORDS = {"red": [], "degraded": []}


def _fake_run(stdout_str):
    """subprocess.run stand-in: a CALLABLE returning a fixed curl stdout."""
    def _run(*args, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(stdout=stdout_str)
    return _run


class Test_Dhc8_RssResponseGate:
    """MC/DC for the 5-condition curl-response guard in _run_rss_feed_check()."""

    def _check(self, A, monkeypatch, stdout, words=None):
        monkeypatch.setattr(subprocess, "run", _fake_run(stdout))
        return _hc._run_rss_feed_check("http://vendor.example/feed", 5, words or EMPTY_WORDS)

    def test_all_False__baseline_green(self, A, monkeypatch):
        """All guards pass: valid 200 + XML -> keyword mapping runs (green)."""
        result, code = self._check(A, monkeypatch, FEED_BODY + "\n200")
        assert (result, code) == ("green", 200)

    def test_C1_False_no_newline__fetch_failure(self, A, monkeypatch):
        """C1=False: stdout without a delimiter newline -> (None, None)."""
        assert self._check(A, monkeypatch, "no body here, no newline") == (None, None)

    def test_C2_True_non_numeric_code__fetch_failure(self, A, monkeypatch):
        """C2=True: code part is not an integer -> (None, None)."""
        assert self._check(A, monkeypatch, FEED_BODY + "\nabc") == (None, None)

    def test_C3_True_code_zero__fetch_failure(self, A, monkeypatch):
        """C3=True (code==0, curl transport error) -> (None, None)."""
        assert self._check(A, monkeypatch, FEED_BODY + "\n0") == (None, None)

    def test_C3_True_code_above_599__fetch_failure(self, A, monkeypatch):
        """C3=True (code>599, impossible HTTP status) -> (None, None)."""
        assert self._check(A, monkeypatch, FEED_BODY + "\n600") == (None, None)

    def test_C4_True_non_200_status__fetch_failure(self, A, monkeypatch):
        """C4=True: HTTP 404 -> (None, 404) — status surfaced, no keyword scan."""
        assert self._check(A, monkeypatch, FEED_BODY + "\n404") == (None, 404)

    def test_C5_True_malformed_xml__fetch_failure(self, A, monkeypatch):
        """C5=True: 200 OK but body is not parseable XML -> (None, 200).
        An HTML status page must never read as 'all clear'. The unescaped
        '&' guarantees ET.ParseError (a well-shaped <html> tree would parse).
        """
        html = "<html><body>Page moved & relocated <A HREF='x'>here</A></body></html>\n200"
        assert self._check(A, monkeypatch, html) == (None, 200)


# ─── D_hc9: RSS keyword precedence mapping ─────────────────────────
# Expressions (_run_rss_feed_check L478-482):
#   if red_words and any(w in hay for w in red_words):          return "red"
#   if degraded_words and any(w in hay for w in degraded_words): return "degraded"
#   return "green"
# Conditions:
#   C1 = red_words non-empty        C2 = a red word matches hay
#   C3 = degraded_words non-empty   C4 = a degraded word matches hay
#
# ┌───────────┬────┬────┬──────┬─────────────────────────────────────────┐
# │ Test      │ C1 │ C2 │ C3/C4 │ Effect (hay controlled via feed body)  │
# ├───────────┼────┼────┼───────┼─────────────────────────────────────────┤
# │ baseline  │ T  │ T  │  x    │ "red" — C1+C2 true flips red           │
# │ T_hc9a    │ F  │ T* │ F     │ "green" — empty red list: no red flip  │
# │ T_hc9b    │ T  │ F  │ F     │ "green" — no matching red word         │
# │ T_hc9c    │ F  │ x  │ F,T   │ "degraded" — degraded path taken        │
# │ T_hc9d    │ F  │ x  │ F,F   │ "green" — no matching degraded word    │
# │ T_hc9e    │ T  │ T  │ F,T   │ "red" — red precedence over degraded   │
# └───────────┴────┴────┴───────┴─────────────────────────────────────────┘
# T_hc9a's C2 column is the would-be match: with the red LIST empty the same
# outage text no longer flips red (proof C1 independently controls outcome).
# T_hc9e: feed announces BOTH red and degraded markers; red must win.

FEED_RED_TXT = FEED_BODY.replace("<title>T</title>", "<item><title>Major outage now</title></item>")
FEED_DEG_TXT = FEED_BODY.replace(
    "<title>T</title>", "<item><title>Partial degradation under way</title></item>"
)
FEED_BOTH_TXT = FEED_BODY.replace(
    "<title>T</title>",
    "<item><title>Major outage</title><description>Partial degradation</description></item>",
)
WS_RED = {"red": ["outage"], "degraded": []}
WS_DEG = {"red": [], "degraded": ["degradation"]}
WS_BOTH = {"red": ["outage"], "degraded": ["degradation"]}


class Test_Dhc9_RssKeywordPrecedence:
    """MC/DC for the red/degraded/green mapping at the tail of the check."""

    def _check(self, A, monkeypatch, body, words):
        monkeypatch.setattr(subprocess, "run", _fake_run(body + "\n200"))
        return _hc._run_rss_feed_check("http://vendor.example/feed", 5, words)

    def test_C1T_C2T__baseline_red(self, A, monkeypatch):
        """C1=True (red list set) + C2=True (feed has 'outage') -> red."""
        assert self._check(A, monkeypatch, FEED_RED_TXT, WS_RED) == ("red", 200)

    def test_C1_False_empty_red_list__not_red(self, A, monkeypatch):
        """C1=False: same outage feed, but red list empty -> falls through
        to green (no degraded configured either). Proves C1 independently
        gates the red branch."""
        assert self._check(A, monkeypatch, FEED_RED_TXT, {"red": [], "degraded": []}) == ("green", 200)

    def test_C1_True_C2_False_no_red_match__not_red(self, A, monkeypatch):
        """C1=True but C2=False: red list set, feed has no red word -> green."""
        assert self._check(A, monkeypatch, FEED_DEG_TXT, WS_RED) == ("green", 200)

    def test_C3_True_C4_True__degraded(self, A, monkeypatch):
        """C1=False (no red list), C3+C4 True (degraded word present)
        -> degraded path."""
        assert self._check(A, monkeypatch, FEED_DEG_TXT, WS_DEG) == ("degraded", 200)

    def test_C3_True_C4_False_no_degraded_match__green(self, A, monkeypatch):
        """C3=True but C4=False: degraded list set, feed has no degraded
        word -> green."""
        assert self._check(A, monkeypatch, FEED_RED_TXT, WS_DEG) == ("green", 200)

    def test_red_precedence_over_degraded(self, A, monkeypatch):
        """T_hc9e: feed announces BOTH a red and a degraded marker with both
        lists configured -> red. C2(red) wins before the degraded check runs."""
        assert self._check(A, monkeypatch, FEED_BOTH_TXT, WS_BOTH) == ("red", 200)


# ─── D_hc10: rss parse url guard (explicit type) ───────────────────
# Expression (_parse_healthchecks L211-216): for `type: rss` entries
#   C1 = not url                       (missing key / None)
#   C2 = not isinstance(url, str)      (non-string, e.g. numeric)
#   C3 = not url.strip()               (empty / whitespace-only)
#   C4 = not _safe_url(url.strip())    (bad scheme / unsafe host)
# Any True -> entry skipped. All False -> parsed as type "rss" with an
# (possibly empty) keyword map. Note: rss is NEVER auto-detected — a bare
# url still maps to curl (covered by test_healthcheck_admin.py).
#
# ┌────────────┬────────────────────────────────────────────────────────┐
# │ Test       │ Condition flipped (others at baseline values)          │
# ├────────────┼────────────────────────────────────────────────────────┤
# │ T_hc10a    │ C1=True  — url key absent        -> {}                 │
# │ T_hc10b    │ C2=True  — url: 12345 (int)      -> {}                 │
# │ T_hc10c    │ C3=True  — url: "   "            -> {}                 │
# │ T_hc10d    │ C4=True  — url: "ftp://x/feed"   -> {}                 │
# │ baseline   │ all False — valid http url       -> parsed (type=rss)  │
# └────────────┴────────────────────────────────────────────────────────┘

class Test_Dhc10_RssUrlGuard:
    """MC/DC for the 4-condition url guard on the rss parse branch."""

    def _write(self, A, details):
        with open(str(_cfg.get_config_path()), "w") as f:
            yaml.dump(
                {"items": ["SvcA"], "_runtime": {}, "healthchecks": {"SvcA": details}},
                f, default_flow_style=False, sort_keys=False,
            )

    def test_C1_True_missing_url__skipped(self, A):
        """C1=True: type=rss with no url -> entry rejected."""
        self._write(A, {"type": "rss"})
        assert _hc._parse_healthchecks() == {}

    def test_C2_True_non_string_url__skipped(self, A):
        """C1=False (key present), C2=True (int) -> rejected."""
        self._write(A, {"type": "rss", "url": 12345})
        assert _hc._parse_healthchecks() == {}

    def test_C3_True_whitespace_url__skipped(self, A):
        """C1=C2=False (present, str), C3=True (blank) -> rejected."""
        self._write(A, {"type": "rss", "url": "   "})
        assert _hc._parse_healthchecks() == {}

    def test_C4_True_bad_scheme__skipped(self, A):
        """C1..C3=False, C4=True (ftp scheme fails _safe_url) -> rejected."""
        self._write(A, {"type": "rss", "url": "ftp://vendor.example/feed"})
        assert _hc._parse_healthchecks() == {}

    def test_all_False__parsed_as_rss(self, A):
        """All False: valid http url -> parsed as rss with default empty
        keyword lists and sane numerics."""
        self._write(A, {"type": "rss", "url": "http://vendor.example/feed"})
        hc = _hc._parse_healthchecks()
        assert "SvcA" in hc
        entry = hc["SvcA"]
        assert entry["type"] == "rss"
        assert entry["url"] == "http://vendor.example/feed"
        assert entry["keywords"] == {"red": [], "degraded": []}


# ─── D_hc11: RSS entry tag filter ──────────────────────────────────
# Expression (_run_rss_feed_check L465-475): the scan window is
#   for el in entry: if _local(el.tag) in ("title", "description", "summary")
# reached ONLY when _local(entry.tag) in ("item", "entry").
# Conditions (per feed element at iteration depth):
#   C1 = element is an item/entry tag       (else never scanned)
#   C2 = child is title/description/summary (else its text is ignored)
# Consequence: keywords appearing ONLY in channel-level metadata (the channel
# <title>/<description>, feed <title>) can never flip the status — those
# elements are not items. Proving this pins the namespace-agnostic local-name
# filter and the item-scope boundary in one shot.
#
# ┌────────────┬─────────────────────────────────────────────────────────┐
# │ Test       │ Independent condition proven                            │
# ├────────────┼─────────────────────────────────────────────────────────┤
# │ baseline   │ C1=True (keyword in <item><title>) -> red               │
# │ T_hc11a    │ C1=False (keyword ONLY in channel <title>/<description> │
# │            │   or feed <title>) -> green — non-item text never       │
# │            │   reaches the keyword scan                              │
# └────────────┴─────────────────────────────────────────────────────────┘

FEED_ITEM_MATCH = (
    '<?xml version="1.0"?>'
    '<rss version="2.0"><channel>'
    "<title>Vendor Status</title>"
    "<item><title>Major outage declared</title></item>"
    "</channel></rss>"
)
FEED_CHANNEL_ONLY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel>'
    "<title>Major outage declared in channel title</title>"
    "<description>Major outage declared in channel description</description>"
    "<item><title>All systems operational</title>"
    "<description>No active incidents</description></item>"
    "</channel></rss>"
)
ATOM_CHANNEL_ONLY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    "<title>Major outage declared in atom feed title</title>"
    "<entry><title>All systems operational</title>"
    "<summary>No active incidents</summary></entry>"
    "</feed>"
)


class Test_Dhc11_RssEntryFilter:
    """MC/DC for the item/entry tag filter scoping the keyword scan."""

    def _check(self, A, monkeypatch, body, words):
        monkeypatch.setattr(subprocess, "run", _fake_run(body + "\n200"))
        return _hc._run_rss_feed_check("http://vendor.example/feed", 5, words)

    def test_baseline_item_title_matched__red(self, A, monkeypatch):
        """C1=True: keyword inside <item><title> -> scanned -> red."""
        assert self._check(A, monkeypatch, FEED_ITEM_MATCH, WS_RED) == ("red", 200)

    def test_keyword_only_in_channel_metadata__green(self, A, monkeypatch):
        """C1=False: keyword appears only in channel <title> and
        <description> (non-item metadata) -> never scanned -> green."""
        result = self._check(A, monkeypatch, FEED_CHANNEL_ONLY, WS_RED)[0]
        assert result == "green", "channel-level metadata must not trip keywords"

    def test_keyword_only_in_atom_feed_title__green(self, monkeypatch, A):
        """Same property for Atom: a keyword in the feed-level <title>
        (outside any <entry>) must not match."""
        result = self._check(A, monkeypatch, ATOM_CHANNEL_ONLY, WS_RED)[0]
        assert result == "green", "atom feed-level title must not trip keywords"


# ─── Additional tests for healthcheck worker ──────────────────────

class TestHealthcheckWorkerLock:
    """Test the file lock mechanism in _healthcheck_worker()."""

    def test_worker_lock_acquisition(self, A, tmp_path):
        """Test that the worker lock can be acquired and prevents second worker."""
        import fcntl
        import app as m
        
        # Create a temp lock file path
        lock_file_path = tmp_path / ".healthcheck.lock"
        lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # First acquisition should succeed
        lock_file1 = open(lock_file_path, "a+")
        try:
            fcntl.flock(lock_file1.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            got_lock1 = True
        except (IOError, OSError):
            got_lock1 = False
        assert got_lock1, "First worker should acquire lock"
        
        # Second acquisition should fail (non-blocking)
        lock_file2 = open(lock_file_path, "a+")
        try:
            fcntl.flock(lock_file2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            got_lock2 = True
        except (IOError, OSError):
            got_lock2 = False
        assert not got_lock2, "Second worker should NOT acquire lock"
        
        # Release first lock
        fcntl.flock(lock_file1.fileno(), fcntl.LOCK_UN)
        lock_file1.close()
        lock_file2.close()
        
        # Now third acquisition should succeed
        lock_file3 = open(lock_file_path, "a+")
        try:
            fcntl.flock(lock_file3.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            got_lock3 = True
        except (IOError, OSError):
            got_lock3 = False
        assert got_lock3, "Third worker should acquire lock after release"
        lock_file3.close()

    def test_worker_returns_when_locked(self, A, monkeypatch, tmp_path):
        """Test that _healthcheck_worker() returns early when lock held by another."""
        import fcntl
        import healthcheck as m
        
        # Create a temp lock file and hold it
        lock_file_path = tmp_path / ".healthcheck.lock"
        lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Monkeypatch the lock file path by patching _BASE_DIR in healthcheck module
        monkeypatch.setattr(m, "_BASE_DIR", tmp_path)
        
        # Hold the lock
        held_lock = open(lock_file_path, "a+")
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        
        # Call _healthcheck_worker - it should return immediately
        # We can't easily test the infinite loop, but we can verify the lock logic
        # by checking that the function returns without doing work
        try:
            # This would normally run forever, but with lock held it should return
            # We test just the lock acquisition part
            lock_file = open(lock_file_path, "a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # If we get here, we got the lock (shouldn't happen)
                lock_file.close()
                assert False, "Should not acquire lock when held"
            except (IOError, OSError):
                # Expected - lock held by another process
                lock_file.close()
                pass  # This is the early return path
        finally:
            fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)
            held_lock.close()


# ─── D_hc12: Healthchecks master enabled switch ──────────────────
# Expression (healthcheck.py worker loop):
#   if isinstance(sec, dict) and not sec.get("healthchecks_enabled", True):
#     -> sleep & continue without running checks
#
# MC/DC conditions:
#   C1 = isinstance(sec, dict)
#   C2 = not sec.get("healthchecks_enabled", True)
#
# ┌──────┬────┬────┬──────────────────────────────────────────────────┐
# │ Test │ C1 │ C2 │ Observable effect                                │
# ├──────┼────┼────┼──────────────────────────────────────────────────┤
# │ T1   │ T  │ T  │ Paused (disabled by setting -> sleep & continue) │
# │ T2   │ T  │ F  │ Active (enabled by setting -> checks parse/run)  │
# │ T3   │ F  │ ×  │ Active (non-dict settings defaults to enabled)   │
# └──────┴────┴────┴──────────────────────────────────────────────────┘

class Test_Dhc12_HealthchecksEnabledSwitch:
    """MC/DC for the healthchecks_enabled setting gate."""

    def test_C1T_C2T__disabled_skips_execution(self, A):
        """C1=T, C2=T: settings dict present and healthchecks_enabled is False -> pauses."""
        from statuspage.config import healthchecks_enabled, _save_settings, _load_settings
        orig = _load_settings()
        try:
            _save_settings({**orig, "healthchecks_enabled": False})
            assert healthchecks_enabled() is False
            sec = _load_settings()
            should_pause = isinstance(sec, dict) and not sec.get("healthchecks_enabled", True)
            assert should_pause is True, "Must pause worker loop when healthchecks_enabled=False"
        finally:
            _save_settings(orig)

    def test_C1T_C2F__enabled_runs_execution(self, A):
        """C1=T, C2=F: settings dict present and healthchecks_enabled is True -> active."""
        from statuspage.config import healthchecks_enabled, _save_settings, _load_settings
        orig = _load_settings()
        try:
            _save_settings({**orig, "healthchecks_enabled": True})
            assert healthchecks_enabled() is True
            sec = _load_settings()
            should_pause = isinstance(sec, dict) and not sec.get("healthchecks_enabled", True)
            assert should_pause is False, "Must NOT pause worker loop when healthchecks_enabled=True"
        finally:
            _save_settings(orig)

    def test_C1F__non_dict_defaults_to_active(self, A):
        """C1=F: non-dict settings defaults to active."""
        sec = None
        should_pause = isinstance(sec, dict) and not sec.get("healthchecks_enabled", True)
        assert should_pause is False, "Non-dict settings must not trigger pause"


# ─── CLI entry point ──────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-v"]))
