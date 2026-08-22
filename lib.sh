#!/usr/bin/env bash
# lib.sh — Shared error-reporting helpers for status-my-page scripts.
#
# Source this after `set -euo pipefail`:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$SCRIPT_DIR/lib.sh"
#
# Provides:
#   die <message> [hint]        — print a formatted error (+ optional fix hint) and exit 1
#   warn <message>              — print a warning to stderr
#   step <message>              — section banner
#   ok <message>                — success line
#   run_step <label> <cmd...>   — run cmd; on failure report label + captured log
#   require_cmd <name> <hint>   — fail fast if a command is missing
#   ERR_LOG / trap_error        — global error trap printing the failing line

# Error log: last failing command output is kept here for reporting.
ERR_LOG="${TMPDIR:-/tmp}/status-page-install-error.log"

step()  { printf '\n=== %s ===\n' "$*"; }
ok()    { printf '✔ %s\n' "$*"; }
warn()  { printf '⚠️  %s\n' "$*" >&2; }

die() {
    local msg="$1" hint="${2:-}"
    printf '\n✖ ERROR: %s\n' "$msg" >&2
    if [ -n "$hint" ]; then
        printf '   Fix: %s\n' "$hint" >&2
    fi
    if [ -s "$ERR_LOG" ]; then
        printf '\n   Last command output (also saved in %s):\n' "$ERR_LOG" >&2
        sed 's/^/     | /' "$ERR_LOG" | tail -20 >&2
    fi
    exit 1
}

require_cmd() {
    local name="$1" hint="${2:-Install it with your system package manager.}"
    command -v "$name" &>/dev/null || die "Required command '$name' not found." "$hint"
}

run_step() {
    # run_step <label> <cmd...> — runs the command, tees output to $ERR_LOG.
    local label="$1"; shift
    local tmp
    tmp=$(mktemp)
    if "$@" >"$tmp" 2>&1; then
        rm -f "$tmp"
        ok "$label"
    else
        local rc=$?
        mv "$tmp" "$ERR_LOG" 2>/dev/null || cp "$tmp" "$ERR_LOG" 2>/dev/null || true
        die "$label failed (exit $rc)." \
            "Re-run the install; if it persists, inspect $ERR_LOG."
    fi
}

# Global trap: on any unhandled error, show where it happened.
trap 'rc=$?; [ $rc -ne 0 ] && printf "\n✖ Install aborted at %s line %d (exit %d)\n" "${BASH_SOURCE[1]:-unknown}" ${BASH_LINENO[0]:-0} $rc >&2' ERR
