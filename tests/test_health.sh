#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Health-check test suite for the status page server
#
# Usage:
#   cd ~/Developer/status-my-page && ./tests/test_health.sh              # runs on localhost:8920
#   cd ~/Developer/status-my-page && ./tests/test_health.sh http://myserver:9999  # custom URL
# ──────────────────────────────────────────────────────────────────────-

set -euo pipefail

BASE_URL="${1:-http://localhost:8920}"
COOKIES=$(mktemp)
trap 'rm -f "$COOKIES"' EXIT

PASS=0; FAIL=0

pass_ok()  { PASS=$((PASS+1)); echo "  ✅ $1";            }
fail_fail() { FAIL=$((FAIL+1)); echo "  ❌ $1${2:+ — $2}"; }

echo "Health checks → ${BASE_URL}"
echo '───────────────────────────\n'

# ───────────── 0. Page loads (HTTP 200) + has status rows ─────
RC=$(curl --silent -o /tmp/_hp_body.html \
    -w '%{http_code}' "${BASE_URL}/" 2>/dev/null) || RC=000

[[ "$RC" == "200" ]] && pass_ok "GET / → HTTP 200" \
    || fail_fail "GET / expected 200, got $RC"

BODY=$(cat /tmp/_hp_body.html)

ROWS=$(echo "$BODY" | grep -c 'data-id=' || true)
(( ROWS > 0 )) && pass_ok "$ROWS status rows rendered" \
    || fail_fail "No status rows found on page"

# ───────────── 1. Overall badge visible ────────────────────
echo "$BODY" | grep -q 'overall-badge' && pass_ok "Overall badge element present" \
    || fail_fail "Badge element missing from page"

# ───────────── 2. Static assets return HTTP 200 ─────────────
CSS_RC=$(curl --silent -o /dev/null \
    -w '%{http_code}' "${BASE_URL}/static/css/style.css" 2>/dev/null) || CSS=000

JS_RC=$(curl --silent -o /dev/null \
    -w '%{http_code}' "${BASE_URL}/static/js/app.js"     2>/dev/null) || JS_RC=000

[[ "$CSS_RC" == "200" ]] && pass_ok "GET /static/css/style.css → 200" \
    || fail_fail "style.css expected 200, got $CSS_RC"

[[ "$JS_RC" == "200" ]] && pass_ok "GET /static/js/app.js   → 200" \
    || fail_fail "app.js    expected 200, got $JS_RC"

# ───────────── 3. Auth-check returns JSON with admin field ──────
AUTH=$(curl --silent "${BASE_URL}/auth-check" 2>/dev/null) || AUTH=''

echo "$AUTH" | grep -q '"admin"' && pass_ok "GET /auth-check → JSON { admin: false }" \
    || fail_fail "/auth-check did not return expected format, got: $AUTH"

# ───────────── 4. Login works with default credentials ──────────── (expects admin/changeme)
LOGIN=$(curl --silent -X POST "${BASE_URL}/login" \
    --header 'Content-Type: application/json' \
    --data-raw '{"user":"admin","pass":"changeme"}' 2>/dev/null) || LOGIN=''

echo "$LOGIN" | grep -q '"ok"' && pass_ok "POST /login → success (admin/changeme)" \
    || fail_fail "Login failed: $LOGIN"

# ────────────── 5. Unauthenticated toggle request → HTTP 403 ───
FORBID_RC=$(curl --silent -o /dev/null \
    -w '%{http_code}' -X POST "${BASE_URL}/api/toggle/999" 2>/dev/null) || FORBID_RC=000

[[ "$FORBID_RC" == "403" ]] && pass_ok "Unauthenticated toggle → HTTP 403 (forbidden)" \
    || fail_fail "/api/toggle without auth expected 403, got $FORBID_RC"

# ──────────── 6. Full 3-state cycle via authenticated session ────────
curl --silent -c "$COOKIES" -X POST "${BASE_URL}/login" \
    --header 'Content-Type: application/json' \
    --data-raw '{"user":"admin","pass":"changeme"}' > /dev/null 2>&1

FIRST_ID=$(echo "$BODY" | grep -oP 'data-id="\K[0-9]+' | head -1)

if [[ -z "${FIRST_ID:-}" ]]; then
    fail_fail "Could not extract an item data-id from page HTML";
else
    S1=$(curl --silent -b "$COOKIES" -X POST \
        "${BASE_URL}/api/toggle/${FIRST_ID}" 2>/dev/null | sed 's/.*"status": *"\([^"]*\)".*/\1/')

    S2=$(curl --silent -b "$COOKIES" -X POST \
        "${BASE_URL}/api/toggle/${FIRST_ID}" 2>/dev/null | sed 's/.*"status": *"\([^"]*\)".*/\1/')

    S3=$(curl --silent -b "$COOKIES" -X POST \
        "${BASE_URL}/api/toggle/${FIRST_ID}" 2>/dev/null | sed 's/.*"status": *"\([^"]*\)".*/\1/')

    if [[ "$S1" == "degraded" && "$S2" == "red" && "$S3" == "green" ]]; then
        pass_ok "3-state toggle cycle correct: green→ 🟡 degraded → 🔴 red → 🟢 green"
    else
        fail_fail "Cycle expected degraded→red→green, got $S1→$S2→$S3"
    fi;
fi

# ─────────── 7. Notes API accepts admin input ────────
NOTES_RESP=$(curl --silent -b "$COOKIES" -X POST "${BASE_URL}/api/notes/${FIRST_ID}" \
    --header 'Content-Type: application/json' \
    --data-raw '{"notes":"health-check verification"}' 2>/dev/null) || NOTES_RESP=''

echo "$NOTES_RESP" | grep -q '"ok"' && pass_ok "POST /api/notes accepts data & saves successfully" \
    || fail_fail "Notes API response unexpected, got: $NOTES_RESP"

# ─────────── 4. Summary ──────
TOTAL=$((PASS+FAIL))
echo ""
if (( FAIL == 0 )); then
    echo "🎉 ✅ All ${TOTAL} checks passed!"
else
    echo "⚠️ ❌ ${FAIL} of ${TOTAL} check(s) failed."
fi
