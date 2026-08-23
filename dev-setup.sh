#!/usr/bin/env bash
# dev-setup.sh — Developer quick-start for status-my-page
# Creates a local venv, installs deps, seeds DB, and prints dev-server
# launch instructions.
#
# After a git pull, run this to (re)configure your local environment.
# Prompts interactively for each setup option; defaults keep it fast.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
ENV_FILE="$ROOT_DIR/.env.local"
PY="$VENV_DIR/bin/python3"

source "$ROOT_DIR/lib.sh"

echo "=== status-my-page developer setup ==="
echo "Root: $ROOT_DIR"

# ── 0. Git sync (safe pull) ─────────────────────────────────────────
step "Git sync"
if git -C "$ROOT_DIR" rev-parse --git-dir > /dev/null 2>&1; then
    BRANCH=$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)
    if [ "$BRANCH" = "HEAD" ]; then
        warn "Detached HEAD — skipping pull."
    else
        if git -C "$ROOT_DIR" diff --quiet && git -C "$ROOT_DIR" diff --cached --quiet; then
            run_step "git pull --ff-only (branch $BRANCH)" \
                git -C "$ROOT_DIR" pull --ff-only
        else
            warn "Uncommitted local changes — skipping pull to avoid conflicts."
            echo "    Commit or stash, then re-run this script to pull."
        fi
    fi
else
    warn "Not a git repository — skipping pull."
fi

# ── 1. Virtual environment + dependencies ───────────────────────────
step "Python virtual environment"
if [ ! -d "$VENV_DIR" ]; then
    run_step "create virtualenv" python3 -m venv "$VENV_DIR"
fi
run_step "upgrade pip" "$VENV_DIR/bin/pip" install --upgrade pip --quiet
run_step "install requirements" "$VENV_DIR/bin/pip" \
    install -r "$ROOT_DIR/requirements.txt" --quiet
ok "Dependencies installed"

# ── 2. Interactive configuration prompts ────────────────────────────
# Runs after the git pull so a fresh clone gets prompted for everything;
# existing values are offered as defaults so re-runs are Enter-heavy.

step "Configuration"

# Load any previous values as defaults
DEFAULT_USER="admin"
DEFAULT_PORT="8920"
DEFAULT_HC="1"
DEFAULT_LOGO=""
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source <(grep -E '^(DEV_ADMIN_USER|DEV_PORT|DEV_DISABLE_HC|DEV_LOGO_PATH)=' "$ENV_FILE" 2>/dev/null || true) || true
    [ -n "${DEV_ADMIN_USER:-}" ] && DEFAULT_USER="$DEV_ADMIN_USER"
    [ -n "${DEV_PORT:-}" ] && DEFAULT_PORT="$DEV_PORT"
    [ -n "${DEV_DISABLE_HC:-}" ] && DEFAULT_HC="$DEV_DISABLE_HC"
    [ -n "${DEV_LOGO_PATH:-}" ] && DEFAULT_LOGO="$DEV_LOGO_PATH"
fi

# 2a. Admin username
read -rp "Admin username [$DEFAULT_USER]: " ADMIN_USER_INPUT
ADMIN_USER="${ADMIN_USER_INPUT:-$DEFAULT_USER}"

# 2b. Admin password (only if we need to (re)write the env file)
NEED_ENV=1
if [ -f "$ENV_FILE" ] && grep -q '^STATUS_ADMIN_PASS_HASH=' "$ENV_FILE"; then
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
while true; do
    read -rp "Dev server port [$DEFAULT_PORT]: " PORT_INPUT
    PORT="${PORT_INPUT:-$DEFAULT_PORT}"
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
read -rp "Logo file to install (blank to skip) [${DEFAULT_LOGO}]: " LOGO_INPUT
LOGO_PATH="${LOGO_INPUT:-$DEFAULT_LOGO}"

echo ""

# ── 3. Write env file (single-quoted values — see review fix c7571cc) ─
step "Environment file"

if [ "$NEED_ENV" -eq 1 ]; then
    PASS_HASH="$("$PY" -c "
import sys
from werkzeug.security import generate_password_hash
print(generate_password_hash(sys.stdin.read().rstrip('\n')))")" \
        <<< "$ADMIN_PASS" 2>/dev/null || true
    if [ -z "$PASS_HASH" ]; then
        die "Failed to generate password hash." \
            "Re-run the script; check that the venv has werkzeug installed."
    fi
    ok "Password hash generated"
else
    # Reset declined — reuse the stored hash verbatim.
    PASS_HASH=$(grep '^STATUS_ADMIN_PASS_HASH=' "$ENV_FILE" 2>/dev/null \
                | sed "s/^STATUS_ADMIN_PASS_HASH='//;s/'$//" || true)
    if [ -z "$PASS_HASH" ]; then
        die "Could not resolve a password hash." \
            "Delete $ENV_FILE and re-run to start fresh."
    fi
    ok "Reusing existing password hash"
fi

SECRET_KEY=$(grep '^STATUS_SECRET_KEY=' "$ENV_FILE" 2>/dev/null \
             | sed "s/^STATUS_SECRET_KEY='//;s/'$//" || true)
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
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "Wrote $ENV_FILE"

# ── 4. Database ─────────────────────────────────────────────────────
step "Database"
cd "$ROOT_DIR"
export STATUS_DISABLE_HEALTHCHECKS="$DEV_DISABLE_HC"
# init_db imports app, which requires the admin hash at import time —
# source the env file we just wrote.
set -a
# shellcheck disable=SC1091
source "$ENV_FILE"
set +a
run_step "seed database (init_db)" "$PY" -c "
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
echo "  source $VENV_DIR/bin/activate"
echo "  flask --app app run --host 0.0.0.0 --port $PORT"
echo ""
echo "Or:  ./start.sh   (uses .env.local; port from config.yaml)"
