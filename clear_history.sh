#!/usr/bin/env bash
# clear_history.sh — Clear the status-change history timeline via the admin API
#
# Usage:
#   ./clear_history.sh list                    # all services + entry count / last change
#   ./clear_history.sh show <id>               # print one service's timeline
#   ./clear_history.sh clear <id>              # clear one service's history
#   ./clear_history.sh clear-all               # clear history for every service
#   ./clear_history.sh <command> --yes         # skip the confirmation prompt
#   ./clear_history.sh <command> [url]         # optional server URL (default http://localhost:8920)
#
# Auth: admin session over HTTP (POST /login). Plaintext password comes from
# HEALTH_PASS (and optionally HEALTH_USER, default "admin") — same convention
# as tests/test_health.sh; the app has no default password. Set them in the
# environment or in .env.local (written by install.sh / dev-setup.sh).
#
# Requires a running server (./start.sh). Works whether or not the public
# history feature is enabled — the clear endpoint is admin+CSRF only and is
# not gated by the history setting.
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

usage() {
    sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

# ── Args: subcommand, --yes, optional URL ───────────────────────────
COMMAND=""
ASSUME_YES=0
URLS=()
ITEM_ID_ARG=""
for arg in "$@"; do
    case "$arg" in
        -h|--help) usage ;;
        list|show|clear|clear-all) COMMAND="$arg"; PREV="" ;;
        --yes|-y) ASSUME_YES=1 ;;
        *:[0-9]*|*)
            # Heuristic: numeric = item id, starts with http = URL
            if [[ "$arg" =~ ^[0-9]+$ && -z "$ITEM_ID_ARG" ]]; then
                ITEM_ID_ARG="$arg"
            elif [[ "$arg" =~ ^https?:// ]]; then
                URLS+=("$arg")
            else
                warn "Ignoring unrecognized argument: $arg"
            fi
            ;;
    esac
done
[ -n "$COMMAND" ] || usage
case "$COMMAND" in
    show|clear)
        [ -n "$ITEM_ID_ARG" ] || { printf '✖ %s requires an item id (see: ./clear_history.sh list)\n' "$COMMAND" >&2; exit 1; }
        ;;
esac
BASE_URL="${URLS[0]:-http://localhost:8920}"
BASE_URL="${BASE_URL%/}"

# ── Env: .env.local (install.sh/dev-setup.sh) or .env, like start.sh ──
if [ -f .env.local ]; then
    set -a; . ./.env.local; set +a
elif [ -f .env ]; then
    set -a; . ./.env; set +a
fi
USER_NAME="${HEALTH_USER:-${STATUS_ADMIN_USER:-admin}}"
PASS="${HEALTH_PASS:-}"
if [ -z "$PASS" ]; then
    die "HEALTH_PASS is not set (plaintext admin password, e.g. 'export HEALTH_PASS=\"...\"' or add to .env.local)." \
        "The app has no default credential — use the same password you log in with in the browser."
fi

COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT
require_cmd curl "Install curl with your system package manager."
require_cmd python3 "Install python3 with your system package manager."

CSRF_TOKEN=""   # refreshed after login; required for POSTs

# http <METHOD> <path> — session request; POSTs carry the CSRF header.
http() {
    local method="$1" path="$2"
    if [ "$method" = "POST" ]; then
        curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X POST \
            -H "X-CSRF-Token: $CSRF_TOKEN" "${BASE_URL}${path}"
    else
        curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X "$method" "${BASE_URL}${path}"
    fi
}

get_csrf() {
    http GET /api/csrf-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'
}

step "status-my-page: clear_history → ${BASE_URL} (user: ${USER_NAME})"

# ── Login → CSRF ────────────────────────────────────────────────────
LOGIN_RESP="$(curl -sS -c "$COOKIE_JAR" -X POST -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"user": sys.argv[1], "pass": sys.argv[2]}))' "$USER_NAME" "$PASS")" \
    "${BASE_URL}/login")"
echo "$LOGIN_RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)' \
    || die "Login failed: ${LOGIN_RESP}" "Check HEALTH_USER/HEALTH_PASS against the deployment credentials."
ok "Logged in as ${USER_NAME}"

CSRF_TOKEN="$(get_csrf)"
[ -n "$CSRF_TOKEN" ] || die "Empty CSRF token — server unresponsive after login."
ok "CSRF token acquired"

ITEMS_JSON="$(http GET /api/status)"
echo "$ITEMS_JSON" | python3 -c 'import json,sys; json.load(sys.stdin)' \
    || die "Unexpected response from ${BASE_URL}/api/status: ${ITEMS_JSON}"

print_table() {  # id/name/entries/last-change
    ITEMS_JSON="$ITEMS_JSON" BASE_URL="$BASE_URL" python3 - <<'PYEOF'
import json, os, urllib.request

items = json.loads(os.environ["ITEMS_JSON"])
base = os.environ["BASE_URL"]
print(f"{'ID':<5} {'Service':<30} {'Entries':<9} Last change (UTC)")
print("-" * 70)
for it in items:
    name, count, last = it["name"], "-", "-"
    try:
        with urllib.request.urlopen(f"{base}/api/history/{it['id']}", timeout=5) as r:
            data = json.load(r)
        entries = data.get("entries", [])
        count = str(len(entries))
        if entries:
            last = entries[0].get("occurred", "-")[:19].replace("T", " ") + "Z"
    except Exception:
        count = "n/a"   # history feature disabled (endpoint 404s)
    print(f"{it['id']:<5} {name[:30]:<30} {count:<9} {last}")
PYEOF
}

case "$COMMAND" in
    list)
        print_table
        ;;
    show)
        HISTORY_RESP="$(http GET "/api/history/${ITEM_ID_ARG}")"
        if echo "$HISTORY_RESP" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
            HISTORY_RESP="$HISTORY_RESP" python3 - <<'PYEOF'
import json, os, sys

data = json.loads(os.environ["HISTORY_RESP"])
print(f"Service: {data.get('service', '?')}")
print()
print(f"{'Occurred (UTC)':<24} {'Event':<10} Change")
print("-" * 70)
for e in data.get("entries", []):
    oc = e.get("occurred", "-")[:19].replace("T", " ")
    print(f"{oc:<24} {e.get('event_type', '-'):10} {e.get('old_value', '-')} -> {e.get('new_value', '-')}")
PYEOF
        else
            die "History for item ${ITEM_ID_ARG} is not available." \
                "The history feature may be disabled (🕙 in Page Settings) or the id is wrong — see ./clear_history.sh list."
        fi
        ;;
    clear|clear-all)
        if [ "$COMMAND" = "clear" ]; then
            TARGETS="$ITEM_ID_ARG"
        else
            TARGETS="$(echo "$ITEMS_JSON" | python3 -c '
import json, sys
print(" ".join(str(it["id"]) for it in json.load(sys.stdin)))')"
            [ -n "$TARGETS" ] || { ok "No services configured — nothing to clear."; exit 0; }
        fi

        print_table | tail -n +3
        printf '\nThis permanently deletes the history rows for: %s\n' "$TARGETS"
        if [ "$ASSUME_YES" -ne 1 ]; then
            printf 'Continue? [y/N] '
            read -r ans || true
            [[ "$ans" =~ ^[Yy]$ ]] || { warn "Aborted — nothing deleted."; exit 0; }
        fi

        FAIL=0; TOTAL=0
        for ID in $TARGETS; do
            # Token is single-use (server rotates it on every accepted CSRF
            # request) — re-fetch before each mutation.
            CSRF_TOKEN="$(get_csrf)"
            RESP="$(http POST "/api/history/${ID}/clear")"
            REMOVED="$(echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("removed", "0"))' 2>/dev/null || echo)"
            if [ -n "$REMOVED" ]; then
                TOTAL=$((TOTAL + REMOVED))
                ok "Item $ID: removed $REMOVED entries"
            else
                warn "Item $ID: clear failed → $RESP (history-disabled 404?)"
                FAIL=1
            fi
        done
        printf '\n✔ Cleared %s history entries' "$TOTAL"
        [ "$FAIL" -eq 0 ] && printf ' (all requested services).\n' || printf ' — some services failed.\n'
        exit "$FAIL"
        ;;
esac
