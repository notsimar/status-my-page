#!/usr/bin/env bash
# dev-setup.sh — Developer quick-start for status-my-page
#
# Usage:
#   ./dev-setup.sh [OPTIONS]
#
# Creates a local venv, installs deps, seeds the DB, and prints dev-server
# launch instructions. After a git pull, run this to (re)configure your
# local environment. Interactive by default; existing values are offered
# as defaults so re-runs are Enter-heavy.
#
# Options (non-interactive when --admin-pass or CI=1 is supplied):
#   --admin-user USER   Admin username        (env: SP_ADMIN_USER, default admin)
#   --admin-pass PASS   Admin password        (env: SP_ADMIN_PASS)
#   --port N            Dev server port       (default 8920)
#   --enable-hc         Enable background healthchecks (default: disabled in dev)
#   --logo PATH         Install a logo file
#   --no-pull           Skip the git pull step
#   -h | --help         Show this help
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
ENV_FILE="$ROOT_DIR/.env.local"
PY="$VENV_DIR/bin/python3"

# shellcheck source=lib.sh
source "$ROOT_DIR/lib.sh"

usage() {
    sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

echo "=== status-my-page developer setup ==="
echo "Root: $ROOT_DIR"

# ── Argument parsing ─────────────────────────────────────────────────
ADMIN_USER="${SP_ADMIN_USER:-}"
ADMIN_PASS="${SP_ADMIN_PASS:-}"
PORT=""
ENABLE_HC=0      # dev default: healthchecks off
LOGO_PATH=""
DO_PULL=1

while [ $# -gt 0 ]; do
    case "$1" in
        --admin-user) ADMIN_USER="$2"; shift 2 ;;
        --admin-pass) ADMIN_PASS="$2"; shift 2 ;;
        --port)       PORT="$2"; shift 2 ;;
        --enable-hc)  ENABLE_HC=1; shift ;;
        --logo)       LOGO_PATH="$2"; shift 2 ;;
        --no-pull)    DO_PULL=0; shift ;;
        -h|--help)    usage ;;
        -*)           die "Unknown option: $1" "Run ./dev-setup.sh --help" ;;
        *)            die "Unexpected argument: $1" "Run ./dev-setup.sh --help" ;;
    esac
done

NONINTERACTIVE=0
if [ -n "$ADMIN_PASS" ] || [ "${CI:-0}" = "1" ]; then
    NONINTERACTIVE=1
fi

# ── 0. Git sync (safe pull) ─────────────────────────────────────────
step "Git sync"
if [ "$DO_PULL" -eq 1 ]; then
    if git -C "$ROOT_DIR" rev-parse --git-dir > /dev/null 2>&1; then
        BRANCH=$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)
        if [ "$BRANCH" = "HEAD" ]; then
            warn "Detached HEAD — skipping pull."
        elif git -C "$ROOT_DIR" diff --quiet && git -C "$ROOT_DIR" diff --cached --quiet; then
            run_step "git pull --ff-only (branch $BRANCH)" \
                git -C "$ROOT_DIR" pull --ff-only
        else
            warn "Uncommitted local changes — skipping pull to avoid conflicts."
            echo "    Commit or stash, then re-run this script to pull."
        fi
    else
        warn "Not a git repository — skipping pull."
    fi
else
    ok "Pull skipped (--no-pull)"
fi

# ── 1. Virtual environment + dependencies ───────────────────────────
step "Python virtual environment"
if [ ! -x "$PY" ]; then
    if [ -d "$VENV_DIR" ]; then
        warn "Existing venv is broken (no python binary) — recreating."
        rm -rf "$VENV_DIR"
    fi
    run_step "create virtualenv" python3 -m venv "$VENV_DIR"
fi

# Ensure pip is available inside the venv (handles macOS/Linux distros where venv lacks pip)
if [ ! -x "$VENV_DIR/bin/pip" ]; then
    if "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then
        ok "pip installed via ensurepip"
    fi
fi

# Try upgrading pip quietly (ignore network 403 / offline errors)
"$PY" -m pip install --upgrade pip --quiet 2>/dev/null || true

# Install dependencies with offline vendor/ wheels preferred to prevent 403 PyPI network blocks
if [ -d "$ROOT_DIR/vendor" ] && [ -n "$(ls -A "$ROOT_DIR/vendor"/*.whl 2>/dev/null)" ]; then
    run_step "install requirements (from vendor wheels)" \
        "$PY" -m pip install --no-index --find-links "$ROOT_DIR/vendor" -r "$ROOT_DIR/requirements.txt" --quiet 2>/dev/null || \
    run_step "install requirements (with online fallback)" \
        "$PY" -m pip install --find-links "$ROOT_DIR/vendor" -r "$ROOT_DIR/requirements.txt" --quiet
else
    run_step "install requirements" "$PY" -m pip \
        install -r "$ROOT_DIR/requirements.txt" --quiet
fi
ok "Dependencies installed"

# ── 2. Configuration ────────────────────────────────────────────────
step "Configuration"

# Load previous values as defaults (interactive mode only).
DEFAULT_USER="admin"
DEFAULT_PORT="8920"
LOGO_PREV=""
if [ -f "$ENV_FILE" ] && [ "$NONINTERACTIVE" -eq 0 ]; then
    # Values are single-quoted where they may contain specials; source only
    # the known-good DEV_* keys we wrote ourselves.
    # shellcheck disable=SC1090
    source <(grep -E '^(DEV_ADMIN_USER|DEV_PORT|DEV_DISABLE_HC|DEV_LOGO_PATH)=' "$ENV_FILE" 2>/dev/null || true) || true
    [ -n "${DEV_ADMIN_USER:-}" ] && DEFAULT_USER="$DEV_ADMIN_USER"
    [ -n "${DEV_PORT:-}" ] && DEFAULT_PORT="$DEV_PORT"
    LOGO_PREV="${DEV_LOGO_PATH:-}"
fi

if [ "$NONINTERACTIVE" -eq 1 ]; then
    # Flags / env win; fall back to previous config.
    ADMIN_USER="${ADMIN_USER:-$DEFAULT_USER}"
    PORT="${PORT:-$DEFAULT_PORT}"
    DEV_DISABLE_HC=$((1 - ENABLE_HC))
    LOGO_PATH="${LOGO_PATH:-$LOGO_PREV}"
    NEED_ENV=1
    if [ -z "$ADMIN_PASS" ] && creds_in_env; then
        NEED_ENV=0
        ok "Existing password hash kept (no new password supplied)."
    fi
else
    # 2a. Admin username
    read -rp "Admin username [$DEFAULT_USER]: " ADMIN_USER_INPUT
    ADMIN_USER="${ADMIN_USER_INPUT:-$DEFAULT_USER}"

    # 2b. Admin password (only if we need to (re)write the env file)
    NEED_ENV=1
    if creds_in_env; then
        read -rp "Admin password is already configured. Reset it? [y/N]: " RESET_PW
        if [[ ! "$RESET_PW" =~ ^[Yy] ]]; then
            NEED_ENV=0
            echo "  Keeping existing password hash."
        fi
    fi

    ADMIN_PASS=""
    if [ "$NEED_ENV" -eq 1 ]; then
        while true; do
            read -rsp "Admin password (dev): " ADMIN_PASS
            echo ""
            if [ -z "$ADMIN_PASS" ]; then
                warn "Password cannot be empty — try again."
                continue
            fi
            read -rsp "Confirm password: " ADMIN_PASS2
            echo ""
            [ "$ADMIN_PASS" = "$ADMIN_PASS2" ] && break
            warn "Passwords do not match — try again."
        done
    fi

    # 2c. Dev server port
    PORT="${PORT:-}"
    while true; do
        read -rp "Dev server port [$DEFAULT_PORT]: " PORT_INPUT
        PORT="${PORT_INPUT:-${PORT:-$DEFAULT_PORT}}"
        if [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ]; then
            break
        fi
        warn "Port must be a number between 1 and 65535."
    done

    # 2d. Healthchecks on this dev instance?
    read -rp "Disable background healthchecks for dev? [Y/n]: " HC_INPUT
    HC_INPUT="${HC_INPUT:-Y}"
    case "$HC_INPUT" in
        [Nn]) DEV_DISABLE_HC=0 ;;
        *)    DEV_DISABLE_HC=1 ;;
    esac

    # 2e. Optional logo
    read -rp "Logo file to install (blank to skip) [${LOGO_PREV}]: " LOGO_INPUT
    LOGO_PATH="${LOGO_INPUT:-$LOGO_PREV}"
fi

echo ""

# ── 3. Write env file (single-quoted values — see review fix c7571cc) ─
step "Environment file"

if [ "$NEED_ENV" -eq 1 ]; then
    if ! "$PY" -c "import werkzeug.security" 2>/dev/null; then
        warn "werkzeug not found in $VENV_DIR — installing requirements..."
        "$PY" -m pip install -r "$ROOT_DIR/requirements.txt" --quiet 2>/dev/null || \
        "$PY" -m pip install werkzeug --quiet 2>/dev/null || true
    fi

    export _SP_PASS="$ADMIN_PASS"
    PASS_HASH="$("$PY" - << 'PYEOF'
import os, sys
pwd = os.environ.get('_SP_PASS', '')
try:
    from werkzeug.security import generate_password_hash
    print(generate_password_hash(pwd))
except Exception:
    import hashlib, secrets
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pwd.encode('utf-8'), salt=salt.encode('utf-8'), n=32768, r=8, p=1, maxmem=64*1024*1024).hex()
    print('scrypt:32768:8:1$' + salt + '$' + h)
PYEOF
)"
    unset _SP_PASS
    if [ -z "$PASS_HASH" ]; then
        die "Failed to generate password hash." \
            "Re-run the script; check that the venv has werkzeug installed."
    fi
    ok "Password hash generated"
else
    # Reset declined — reuse the stored hash verbatim.
    PASS_HASH=$(grep '^STATUS_ADMIN_PASS_HASH=' "$ENV_FILE" 2>/dev/null \
                | sed "s/^STATUS_ADMIN_PASS_HASH='//;s/'\$//" || true)
    if [ -z "$PASS_HASH" ]; then
        die "Could not resolve a password hash." \
            "Delete $ENV_FILE and re-run to start fresh."
    fi
    ok "Reusing existing password hash"
fi

SECRET_KEY=$(grep '^STATUS_SECRET_KEY=' "$ENV_FILE" 2>/dev/null \
             | sed "s/^STATUS_SECRET_KEY='//;s/'\$//" || true)
if [ -z "$SECRET_KEY" ]; then
    SECRET_KEY="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
fi

{
    printf 'STATUS_DISABLE_HEALTHCHECKS=%s\n' "$DEV_DISABLE_HC"
    printf "STATUS_ADMIN_PASS_HASH='%s'\n" "$PASS_HASH"
    printf "STATUS_SECRET_KEY='%s'\n" "$SECRET_KEY"
    printf 'DEV_ADMIN_USER=%s\n' "$ADMIN_USER"
    printf 'DEV_PORT=%s\n' "$PORT"
    printf 'DEV_LOGO_PATH=%s\n' "$LOGO_PATH"
} > "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "Wrote $ENV_FILE"

# ── 4. Database ─────────────────────────────────────────────────────
step "Database"
cd "$ROOT_DIR"
export STATUS_DISABLE_HEALTHCHECKS="$DEV_DISABLE_HC"
# init_db imports app, which requires the admin hash at import time —
# load the env file we just wrote via dotenv (same path as app.py).
DOTENV_HASH=$(dotenv_key "$ENV_FILE" STATUS_ADMIN_PASS_HASH)
run_step "seed database (init_db)" env STATUS_ADMIN_PASS_HASH="$DOTENV_HASH" \
    "$PY" -c "
import sys
sys.path.insert(0, '$ROOT_DIR')
from app import init_db
init_db()
print('DB initialized')"

# ── 5. Optional logo ────────────────────────────────────────────────
if [ -n "$LOGO_PATH" ] && [ -f "$LOGO_PATH" ]; then
    step "Logo"
    if [ -x "$ROOT_DIR/scripts/install_logo.sh" ]; then
        run_step "install logo" bash "$ROOT_DIR/scripts/install_logo.sh" \
            "$LOGO_PATH" "$ROOT_DIR"
    else
        warn "scripts/install_logo.sh not found — skipping logo install."
    fi
elif [ -n "$LOGO_PATH" ]; then
    warn "Logo file not found: $LOGO_PATH — skipped."
fi

# ── 6. Summary ──────────────────────────────────────────────────────
echo ""
echo "========================================"
echo "  Setup complete!"
echo "========================================"
echo "  Admin user:     $ADMIN_USER"
echo "  Dev port:       $PORT"
echo "  Healthchecks:   $([ "$DEV_DISABLE_HC" = "1" ] && echo disabled || echo enabled)"
echo "  Logo:           ${LOGO_PATH:-none}"
echo ""
echo "Start the dev server:"
echo "  cd $ROOT_DIR"
echo "  set -a; source .env.local; set +a"
echo "  .venv/bin/flask --app app run --host 127.0.0.1 --port $PORT"
echo ""
echo "Or:  ./start.sh   (gunicorn on port from config.yaml; uses .env.local)"
