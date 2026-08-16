#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Health-check test suite — smoke tests for the status page server
#
# Validates that the server is healthy, pages render correctly, static
# assets are served, and all top-level API endpoints (auth, toggle,
# notes) respond as expected. Designed to be fast enough for CI pipelines
# and post-deploy validation.
#
# Health-check matrix:
#   ┌────┬───────────────────────────────────────────────────────┐
#   │ID  │ What it validates                                     │
#   ├────┼───────────────────────────────────────────────────────┤
#   │ H0 │ Root page (`GET /`) returns HTTP 200 with rendered    │
#   │     │ status rows (data-id attrs)                           │
#   │ H1 │ Overall badge ("overall-badge" DOM element) present    │
#   │ H2 │ Static assets load: /static/css/style.css → 200       │
#   │     │ and /static/js/app.js → 200                            │
#   │ H3 │ Auth-check endpoint (`GET /auth-check`) returns        │
#   │     │ JSON containing an "admin" boolean key                 │
#   │ H4 │ Login endpoint (`POST /login`) with correct            │
#   │     │ credentials returns {"ok": true}                       │
#   │ H5 │ Unauthenticated mutation request → HTTP 403 forbidden  │
#   │ H6 │ Full 3-state cycle works: green → degraded → red → gree│
#   │ H7 │ Notes API (`POST /api/notes/<id>`) accepts and saves    │
#   │     │ user-supplied note text successfully                   │
#   └────┴───────────────────────────────────────────────────────┘
#
# Prerequisites:
#   1. Server running on target URL (see README → Installation)
#   2. Admin credentials supplied — the app refuses to start without
#      STATUS_ADMIN_PASS_HASH (no fallback). Set HEALTH_PASS (and optionally
#      HEALTH_USER) in the environment or .env, matching your deployment.
#
# Usage:
#     cd ~/Developer/status-my-page && ./tests/test_health.sh              # localhost:8920
#     cd ~/Developer/status-my-page && ./tests/test_health.sh http://myserver:9920  # custom URL / port
#
# Exit code:
#   0 = all checks passed, non-zero = one or more failures
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

BASE_URL="${1:-http://localhost:8920}"
COOKIES=$(mktemp)
trap 'rm -f "$COOKIES"' EXIT

PASS=0; FAIL=0

pass_ok()  { PASS=$((PASS+1)); echo "  ✅ $1";            }
fail_fail() { FAIL=$((FAIL+1)); echo "  ❌ $1${2:+ — $2}"; }

# ───────────── Overridable credentials: read from .env if present ────────
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HEALTH_USER="admin"

# Read password from .env file or environment.
# The hash value can contain '$' (e.g. scrypt salt), which would break a bare
# 'source' under 'set -u' — source with nounset temporarily disabled.
if [ -f "$SCRIPT_DIR/.env" ]; then
    set +u
    # shellcheck source=/dev/null
    . "$SCRIPT_DIR/.env"
    set -u
fi

# Use STATUS_ADMIN_USER from env, or fall back to "admin" (username only)
HEALTH_USER="${STATUS_ADMIN_USER:-$HEALTH_USER}"

# No password fallback: the app has no default credential (admin.password /
# changeme were removed). HEALTH_PASS must be supplied in the environment or
# .env — fail fast here rather than guessing.
HEALTH_PASS="${HEALTH_PASS:-}"
if [ -z "$HEALTH_PASS" ]; then
    echo "ERROR: HEALTH_PASS is not set." >&2
    echo "  The status page has no default password. Export your admin password" >&2
    echo "  (e.g. 'export HEALTH_PASS=\"...\"') or add it to $SCRIPT_DIR/.env, then re-run." >&2
    exit 2
fi

echo "Health checks → ${BASE_URL}  (user: ${HEALTH_USER})"
echo '───────────────────────────'
echo ''

# ───────────── H0. Page loads (HTTP 200) + has status rows ────────────
RC=$(curl --silent -o /tmp/_hp_body.html \
    -w '%{http_code}' "${BASE_URL}/" 2>/dev/null) || RC=000

[[ "$RC" == "200" ]] && pass_ok "GET / → HTTP 200" \
    || fail_fail "GET / expected 200, got $RC"

BODY=$(cat /tmp/_hp_body.html)

ROWS=$(echo "$BODY" | grep -c 'data-id=' || true)
(( ROWS > 0 )) && pass_ok "$ROWS status rows rendered" \
    || fail_fail "No status rows found on page"

# ───────────── H1. Overall badge visible ────────────────────
echo "$BODY" | grep -q 'overall-badge' && pass_ok "Overall badge element present" \
    || fail_fail "Badge element missing from page"

# ───────────── H2. Static assets return HTTP 200 ─────────────
CSS_RC=$(curl --silent -o /dev/null \
    -w '%{http_code}' "${BASE_URL}/static/css/style.css" 2>/dev/null) || CSS=000

JS_RC=$(curl --silent -o /dev/null \
    -w '%{http_code}' "${BASE_URL}/static/js/app.js"     2>/dev/null) || JS_RC=000

[[ "$CSS_RC" == "200" ]] && pass_ok "GET /static/css/style.css → 200" \
    || fail_fail "style.css expected 200, got $CSS_RC"

[[ "$JS_RC" == "200" ]] && pass_ok "GET /static/js/app.js   → 200" \
    || fail_fail "app.js    expected 200, got $JS_RC"

# ───────────── H3. Auth-check returns JSON with admin field ──────
AUTH=$(curl --silent "${BASE_URL}/auth-check" 2>/dev/null) || AUTH=''

echo "$AUTH" | grep -q '"admin"' && pass_ok "GET /auth-check → JSON { admin: false }" \
    || fail_fail "/auth-check did not return expected format, got: $AUTH"

# ───────────── H4. Login works with configured credentials ────────
LOGIN=$(curl --silent -X POST "${BASE_URL}/login" \
    --header 'Content-Type: application/json' \
    --data-raw "{\"user\":\"${HEALTH_USER}\",\"pass\":\"${HEALTH_PASS}\"}" 2>/dev/null) || LOGIN=''

echo "$LOGIN" | grep -q '"ok"' && pass_ok "POST /login → success (${HEALTH_USER}/${HEALTH_PASS})" \
    || fail_fail "Login failed: $LOGIN"

# ───────────── H5. Unauthenticated toggle request → HTTP 403 ───
FORBID_RC=$(curl --silent -o /dev/null \
    -w '%{http_code}' -X POST "${BASE_URL}/api/toggle/999" 2>/dev/null) || FORBID_RC=000

[[ "$FORBID_RC" == "403" ]] && pass_ok "Unauthenticated toggle → HTTP 403 (forbidden)" \
    || fail_fail "/api/toggle without auth expected 403, got $FORBID_RC"

# ───────────── H6. Full 3-state cycle via authenticated session ────
curl --silent -c "$COOKIES" -X POST "${BASE_URL}/login" \
    --header 'Content-Type: application/json' \
    --data-raw "{\"user\":\"${HEALTH_USER}\",\"pass\":\"${HEALTH_PASS}\"}" > /dev/null 2>&1

get_csrf() {
    curl --silent -b "$COOKIES" "${BASE_URL}/api/csrf-token" 2>/dev/null | grep -oP '"token":\s*"\K[a-f0-9]+' || echo ""
}

FIRST_ID=$(echo "$BODY" | grep -oP 'data-id="\K[0-9]+' | head -1)

if [[ -z "${FIRST_ID:-}" ]]; then
    fail_fail "Could not extract an item data-id from page HTML";
else
    TOK=$(get_csrf)
    S1=$(curl --silent -b "$COOKIES" -H "X-CSRF-Token: $TOK" -X POST \
        "${BASE_URL}/api/toggle/${FIRST_ID}" 2>/dev/null | sed 's/.*"status": *"\([^"]*\)".*/\1/')

    TOK=$(get_csrf)
    S2=$(curl --silent -b "$COOKIES" -H "X-CSRF-Token: $TOK" -X POST \
        "${BASE_URL}/api/toggle/${FIRST_ID}" 2>/dev/null | sed 's/.*"status": *"\([^"]*\)".*/\1/')

    TOK=$(get_csrf)
    S3=$(curl --silent -b "$COOKIES" -H "X-CSRF-Token: $TOK" -X POST \
        "${BASE_URL}/api/toggle/${FIRST_ID}" 2>/dev/null | sed 's/.*"status": *"\([^"]*\)".*/\1/')

    if [[ "$S1" == "degraded" && "$S2" == "red" && "$S3" == "green" ]]; then
        pass_ok "3-state toggle cycle correct: green → 🟡 degraded → 🔴 red → 🟢 green"
    else
        fail_fail "Cycle expected degraded→red→green, got ${S1}→${S2}→${S3}"
    fi;
fi

# ───────────── H7. Notes API accepts admin input ───────────
TOK=$(get_csrf)
NOTES_RESP=$(curl --silent -b "$COOKIES" -H "X-CSRF-Token: $TOK" -X POST "${BASE_URL}/api/notes/${FIRST_ID}" \
    --header 'Content-Type: application/json' \
    --data-raw '{"notes":"health-check verification"}' 2>/dev/null) || NOTES_RESP=''

echo "$NOTES_RESP" | grep -q '"ok"' && pass_ok "POST /api/notes accepts data & saves successfully" \
    || fail_fail "Notes API response unexpected, got: $NOTES_RESP"

# ───────────── Summary ────────────────
TOTAL=$((PASS+FAIL))
echo ""
if (( FAIL == 0 )); then
    echo "🎉 ✅ All ${TOTAL} checks passed!"
else
    echo "⚠️ ❌ ${FAIL} of ${TOTAL} check(s) failed."
fi

# Exit with appropriate code for CI/CD integration
exit "$((FAIL > 0 ? 1 : 0))"
