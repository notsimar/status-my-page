#!/usr/bin/env python3
"""Centralized input validation and sanitization layer.

Mitigates:
  - XSS: strips/escapes HTML entities, protocol handlers, event attributes
  - SQL injection: detects dangerous SQL keywords/patterns in free text
  - Fuzzing / path traversal: blocks null bytes, control chars, directory traversal
  - Prototype pollution / NoSQL-style attacks (defensive whitelist)
  - Encoding bombs: enforces max length, rejects oversized payloads

Usage:
    from input_filter import sanitize_text, validate_name, validate_notes, validate_user_input

    name = validate_name(raw_data.get("name", ""))   # raises InputRejected on bad input
    notes = validate_notes(raw_data.get("notes", ""))
"""

import re
from enum import Enum, auto
from html import escape as _html_escape


# ── Exception ────────────────────────────────────────────────────────
class InputRejected(Exception):
    """Raised when input fails validation — call sites return 400."""

    def __init__(self, reason: str, field: str = ""):
        self.reason = reason
        self.field = field
        super().__init__(f"Input rejected [{field}]: {reason}")


# ── Limits ───────────────────────────────────────────────────────────
MAX_TEXT_LENGTH = 2000         # hard cap for free-text fields (notes, etc.); mirrors constants.MAX_TEXT_LENGTH
MAX_NAME_LENGTH = 128          # service name
MAX_USERNAME_LENGTH = 64       # login username/password per-field


# ── Pattern blocks ───────────────────────────────────────────────────

# XSS vectors: event handlers, javascript/data/vbscript protocols, <script> tags
XSS_PATTERNS = [
    # Event handler attributes (onclick=, onerror=, onload=, onmouseover=, etc.)
    re.compile(r'\bon\w+\s*=', re.IGNORECASE),
    # javascript:, data:, vbscript: protocol handlers in href/src
    re.compile(r'(javascript|data|vbscript)\s*:', re.IGNORECASE),
    # <script> tags (including encoded/obfuscated variants)
    re.compile(r'<\s*script', re.IGNORECASE),
    # <iframe>, <object>, <embed>, <link>, <meta>, <svg>, <math> — common XSS containers
    re.compile(r'<\s*(?:iframe|object|embed|link|meta|svg|math)', re.IGNORECASE),
    # Expression() or url() in CSS injection contexts
    re.compile(r'(?:expression|url)\s*\(', re.IGNORECASE),
    # Encoded angle brackets that bypass naive filters: &lt; &gt; &#60; &#62;
    re.compile(r'&(?:lt|gt|amp|#\d+|#x[0-9a-f]+);', re.IGNORECASE),
]

# SQL injection patterns, split in two tiers:
#
# ── COMPOUND: definitive multi-keyword attack constructs (UNION SELECT,
#    DROP ... TABLE, OR 1=1 tautologies). Applied to EVERY text field —
#    names, notes, usernames. Deliberately cannot match ordinary operational
#    prose.
SQLI_COMPOUND_PATTERNS = [
    # Classic SQLi: UNION SELECT, OR 1=1, DROP TABLE, etc.
    re.compile(r'\b(?:UNION|SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|EXEC|EXECUTE|TRUNCATE)\b.*\b(?:FROM|WHERE|INTO|TABLE|DATABASE|SET|VALUES)\b', re.IGNORECASE),
    # tautology-based: OR 1=1, OR 'a'='a', AND 1=1
    re.compile(r'''\b(?:OR|AND)\s+['"]?\w+['"]?\s*=\s*['"]?\w+['"]?''', re.IGNORECASE),
]

# ── AGGRESSIVE: single-token indicators (bare `--`, `; word`, 0x hex).
#    Only checked by sanitize_text(), the free-text sanitizer for shell-
#    injection-proximate fields. NOT applied to notes or names: bare
#    semicolons, double-hyphens, and hex strings are legitimate in status
#    notes ("rollback; retry at noon", "v1.2 -- stable release"), and all
#    SQL in this app is parameterized, so this tier adds only friction.
SQLI_AGGRESSIVE_PATTERNS = [
    # Comment-based injection: --, /*, ;DROP
    re.compile(r'(?:--|/\*|\*/|;\s*(?:DROP|DELETE|INSERT|UPDATE|ALTER))', re.IGNORECASE),
    # Stacked queries via semicolons
    re.compile(r';\s*\w+', re.IGNORECASE),
    # Hex encoding often used in SQLi: 0x27, 0x2D2D
    re.compile(r'0x[0-9a-fA-F]{2,}', re.IGNORECASE),
]

# Legacy umbrella: compound + aggressive (sanitize_text uses this).
SQLI_PATTERNS = SQLI_COMPOUND_PATTERNS + SQLI_AGGRESSIVE_PATTERNS

# Path traversal / filesystem escape
PATH_TRAVERSAL = re.compile(r'(?:\.\./|\.\.\\|%2e%2e[%2f\\/]|%5c)', re.IGNORECASE)

# Shell injection indicators (defensive — names/notes shouldn't contain these)
SHELL_INJECTION_PATTERNS = [
    re.compile(r'[`$]'),                          # backtick or $() command substitution
    re.compile(r'(?:&&|\||\|\||;)\s*\w'),         # command chaining: && | || ;
]


# ── Core sanitizers ─────────────────────────────────────────────────

def strip_control_chars(text: str) -> str:
    """Remove null bytes, control characters (except common whitespace)."""
    # Keep \t \n \r as printable whitespace; strip everything else < 0x20
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)


def sanitize_xss(text: str) -> str:
    """HTML-escape all special characters to prevent XSS rendering.

    This is the primary defense for any text that may be rendered in HTML
    contexts where auto-escaping isn't guaranteed (e.g., API JSON responses
    consumed by client-side JS that uses innerHTML).
    """
    return _html_escape(text, quote=True)


def check_xss_patterns(text: str) -> bool:
    """Return True if text contains suspicious XSS patterns. Does NOT modify."""
    for pat in XSS_PATTERNS:
        if pat.search(text):
            return True
    return False


def check_sqli_patterns(text: str) -> bool:
    """Return True if text contains suspicious SQL injection patterns
    (compound + aggressive tiers — the strictest check)."""
    for pat in SQLI_PATTERNS:
        if pat.search(text):
            return True
    return False


def check_sqli_compound(text: str) -> bool:
    """Return True if text contains compound SQL attack constructs only.

    Used by validate_name()/validate_notes() for human-authored text where
    single-token indicators (bare `--`, `; word`, 0x hex) are legitimate —
    all SQL in this app is parameterized, so the aggressive tier would only
    reject real notes for no security gain.
    """
    for pat in SQLI_COMPOUND_PATTERNS:
        if pat.search(text):
            return True
    return False


def check_path_traversal(text: str) -> bool:
    """Return True if text contains directory traversal sequences."""
    return bool(PATH_TRAVERSAL.search(text))


def check_shell_injection(text: str) -> bool:
    """Return True if text contains shell metacharacters."""
    for pat in SHELL_INJECTION_PATTERNS:
        if pat.search(text):
            return True
    return False


# ── General-purpose sanitizer ───────────────────────────────────────

def sanitize_text(text: str, *, max_length: int = MAX_TEXT_LENGTH,
                  strip_html: bool = True, field: str = "input") -> str:
    """General-purpose text sanitizer. Applies all filters and raises on rejection.

    Steps:
      1. Type check (must be str)
      2. Strip control characters / null bytes
      3. Enforce max length
      4. Check for XSS, SQLi, path traversal, shell injection patterns
      5. HTML-escape if requested
      6. Strip leading/trailing whitespace

    Returns the sanitized string or raises InputRejected.
    """
    if not isinstance(text, str):
        raise InputRejected(f"expected string, got {type(text).__name__}", field)

    # Step 1: strip control chars (null bytes, etc.)
    text = strip_control_chars(text)

    # Step 2: enforce length BEFORE pattern checks (saves CPU on bombs)
    if len(text) > max_length:
        raise InputRejected(f"exceeds max length {max_length}", field)

    # Step 3: fuzzing / injection detection (rejection-only, no false blocks
    # on normal text containing SQL keywords incidentally — patterns are compound)
    if check_xss_patterns(text):
        raise InputRejected("contains forbidden characters (XSS)", field)
    if check_sqli_patterns(text):
        raise InputRejected("contains forbidden characters (SQLi)", field)
    if check_path_traversal(text):
        raise InputRejected("contains path traversal", field)
    if check_shell_injection(text):
        raise InputRejected("contains shell metacharacters", field)

    # Step 4: HTML escape
    if strip_html:
        text = sanitize_xss(text)

    # Step 5: whitespace
    text = text.strip()

    return text


# ── Field-specific validators ────────────────────────────────────────

class NameChars(Enum):
    """Allowed character sets for service names."""
    STRICT = auto()       # alphanumeric, spaces, hyphens, underscores only
    RELAXED = auto()      # above + dots, slashes, parens, @


_NAME_PATTERNS = {
    NameChars.STRICT: re.compile(r'^[a-zA-Z0-9 _\-]+$'),
    NameChars.RELAXED: re.compile(r'^[a-zA-Z0-9 _\-\./@()\']+$'),
}


def validate_name(raw: str, field: str = "name",
                  charset: NameChars = NameChars.RELAXED) -> str:
    """Validate a service/display name.

    Enforces: whitelist characters, max length, no injection patterns.
    Does NOT HTML-escape (Jinja/Jackson handles that at render time).
    """
    if not isinstance(raw, str):
        raise InputRejected(f"expected string, got {type(raw).__name__}", field)

    # Strip control chars and check length
    raw = strip_control_chars(raw)
    if len(raw) > MAX_NAME_LENGTH:
        raise InputRejected(f"exceeds max length {MAX_NAME_LENGTH}", field)

    # Injection checks
    for label, check_fn in [
        ("XSS", check_xss_patterns),
        ("SQLi", check_sqli_compound),
        ("path traversal", check_path_traversal),
        ("shell injection", check_shell_injection),
    ]:
        if check_fn(raw):
            raise InputRejected(f"contains forbidden characters ({label})", field)

    # Whitespace + character whitelist
    raw = raw.strip()
    if not raw:
        raise InputRejected("empty after sanitization", field)

    pat = _NAME_PATTERNS[charset]
    if not pat.match(raw):
        raise InputRejected(
            "contains invalid characters (allowed: letters, digits, space, hyphen, underscore)"
            + ("., /, @, (), '" if charset == NameChars.RELAXED else ""),
            field,
        )

    return raw


def validate_notes(raw: str, field: str = "notes") -> str:
    """Validate status notes (free text, longer, no character whitelist).

    Applies injection detection + HTML escaping. Notes are stored in the DB
    and rendered in both Jinja templates (auto-escaped) and JS (escHtml),
    so double-escaping is avoided — we only check for dangerous patterns.
    """
    if not isinstance(raw, str):
        raise InputRejected(f"expected string, got {type(raw).__name__}", field)

    raw = strip_control_chars(raw)
    if len(raw) > MAX_TEXT_LENGTH:
        raise InputRejected(f"exceeds max length {MAX_TEXT_LENGTH}", field)

    for label, check_fn in [
        ("XSS", check_xss_patterns),
        ("SQLi", check_sqli_compound),
        ("path traversal", check_path_traversal),
    ]:
        if check_fn(raw):
            raise InputRejected(f"contains forbidden characters ({label})", field)

    raw = raw.strip()
    return raw


def validate_user_input(raw: str, field: str = "user") -> str:
    """Validate a username field.

    Strict: no injection patterns (usernames are echoed into comparisons and
    logs). Passwords must NOT use this — see validate_password().
    """
    if not isinstance(raw, str):
        raise InputRejected(f"expected string, got {type(raw).__name__}", field)

    raw = strip_control_chars(raw)
    if len(raw) > MAX_USERNAME_LENGTH:
        raise InputRejected(f"exceeds max length {MAX_USERNAME_LENGTH}", field)

    # Injection checks on raw value (before any hashing/processing)
    for label, check_fn in [
        ("XSS", check_xss_patterns),
        ("SQLi", check_sqli_patterns),
        ("path traversal", check_path_traversal),
        ("shell injection", check_shell_injection),
    ]:
        if check_fn(raw):
            raise InputRejected(f"contains forbidden characters ({label})", field)

    return raw.rstrip()  # right-strip only (leading space might be intentional)


def validate_password(raw: str, field: str = "pass") -> str:
    """Validate a password field: length-only policy.

    Deliberately NO pattern filtering: passwords are opaque secrets — `check_password_hash`
    treats the value opaquely, it is never rendered or interpolated, so shell,
    SQLi, and XSS patterns inside a password are harmless. Over-restricting here
    (e.g. rejecting `$`, `&&`, `` ` ``, or `` `OR` ``) made a whole class of
    legitimate passwords unusable (400 at login instead of normal hashing).

    Enforces: must be a string, max MAX_USERNAME_LENGTH chars (kept so
    absurdly long bombs are rejected), control chars stripped.
    """
    if not isinstance(raw, str):
        raise InputRejected(f"expected string, got {type(raw).__name__}", field)

    raw = strip_control_chars(raw)
    if len(raw) > MAX_USERNAME_LENGTH:
        raise InputRejected(f"exceeds max length {MAX_USERNAME_LENGTH}", field)

    return raw.rstrip()  # right-strip only (trailing newline/whitespace rarely intended)


def validate_json_data(data: dict | None) -> dict:
    """Validate that JSON payload is a proper dict, not an unexpected type.

    Guards against prototype pollution-style payloads where the body is
    a list, string, or number instead of an object.
    """
    if data is None:
        raise InputRejected("empty body", "json")
    if not isinstance(data, dict):
        raise InputRejected(f"expected JSON object, got {type(data).__name__}", "json")
    return data


def validate_int_param(value, field: str = "id") -> int:
    """Validate that a value is a clean integer (Flask <int:> does this,
    but for cases where we parse manually from JSON)."""
    if isinstance(value, bool):
        raise InputRejected("boolean is not a valid integer", field)
    if not isinstance(value, int):
        try:
            return int(value)
        except (ValueError, TypeError):
            raise InputRejected(f"not an integer: {value!r}", field)
    if value < 0:
        raise InputRejected("negative value", field)
    return value
