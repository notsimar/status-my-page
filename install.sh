#!/usr/bin/env bash
# install.sh — Deploy status-my-page as an unprivileged user (non-root by default)
#
# Usage:
#   ./install.sh [OPTIONS] [INSTALL_DIR]
#
# Modes:
#   Interactive (default)   — prompts for admin credentials
#   Non-interactive         — pass --admin-user/--admin-pass (or env vars) for
#                             CI/automation; nothing prompts
#   Upgrade                 — re-running against an existing install dir keeps
#                             its .env.local and DB untouched by default
#
# Options:
#   --admin-user USER       Admin username           (env: SP_ADMIN_USER)
#   --admin-pass PASS       Admin password          (env: SP_ADMIN_PASS)
#   --port N                Listen port             (env: STATUS_PORT, default 8920)
#   --host ADDR             Bind address            (env: STATUS_BIND, default 0.0.0.0)
#   --workers N             Gunicorn workers        (default 2)
#   --force-env             Overwrite existing .env.local with supplied creds
#                           (env: SP_INSTALL_OVERRIDE_ENV=1)
#   -h | --help             Show this help
#
# Default INSTALL_DIR = $HOME/.local/share/status-page
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${STATUS_MY_PAGE_ROOT:-$ROOT_DIR}"

# shellcheck source=lib.sh
source "$ROOT_DIR/lib.sh"

usage() {
    sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

# ── Argument parsing ─────────────────────────────────────────────────
INSTALL_DIR=""
ADMIN_USER="${SP_ADMIN_USER:-}"
ADMIN_PASS="${SP_ADMIN_PASS:-}"
STATUS_PORT="${STATUS_PORT:-8920}"
BIND_HOST="${STATUS_BIND:-0.0.0.0}"
GUNICORN_WORKERS=2
FORCE_ENV="${SP_INSTALL_OVERRIDE_ENV:-0}"

while [ $# -gt 0 ]; do
    case "$1" in
        --admin-user)  ADMIN_USER="$2"; shift 2 ;;
        --admin-pass)  ADMIN_PASS="$2"; shift 2 ;;
        --port)        STATUS_PORT="$2"; shift 2 ;;
        --host)        BIND_HOST="$2"; shift 2 ;;
        --workers)     GUNICORN_WORKERS="$2"; shift 2 ;;
        --force-env)   FORCE_ENV=1; shift ;;
        -h|--help)     usage ;;
        -*)            die "Unknown option: $1" "Run ./install.sh --help" ;;
        *)             INSTALL_DIR="$1"; shift ;;
    esac
done

SERVICE_USER="$(whoami)"
SYSTEMD_NAME="status-page.service"
NONINTERACTIVE=0
if [ -n "$ADMIN_USER" ] && [ -n "$ADMIN_PASS" ]; then
    NONINTERACTIVE=1
fi

step "Validating installation path"
case "$INSTALL_DIR" in
    "") INSTALL_DIR="$HOME/.local/share/status-page" ;;
    /*) : ;;
    *)  die "Install path must be absolute: $INSTALL_DIR" \
           "Pass a full path, e.g. $HOME/.local/share/status-page" ;;
esac
case "$INSTALL_DIR" in
    *..*) die "Invalid install path (traversal not allowed): $INSTALL_DIR" \
          "Remove any '..' segments from the path." ;;
esac
INSTALL_DIR=$(normalize_path "$INSTALL_DIR")
export INSTALL_DIR
ok "Install directory: $INSTALL_DIR"

step "Preflight checks"
require_cmd python3 "Install python3 + python3-venv first (e.g. brew install python3 or sudo dnf install python3)."
PYTHON_VER=$(python3 --version | awk '{print $2}' | cut -d. -f1,2)
PYTHON_MAJOR=$(python3 --version | awk '{print $2}' | cut -d. -f1)
PYTHON_MINOR=$(python3 --version | awk '{print $2}' | cut -d. -f2)
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; }; then
    die "Python >= 3.9 required, found $PYTHON_VER." \
        "Install a newer Python 3 and re-run."
fi
ok "Python version: $PYTHON_VER"

if [ "$(id -u)" -eq 0 ]; then
    warn "Running as root. Will attempt system installs and ownership changes."
    ROOTMODE=1
else
    ok "Running as non-root user $SERVICE_USER. Skipping system packages, user creation, systemd."
    ROOTMODE=0
fi

UPGRADE=0
if [ -f "$INSTALL_DIR/config.yaml" ] && [ -d "$INSTALL_DIR/.venv" ]; then
    UPGRADE=1
    step "Existing installation detected"
    ok "Upgrade mode — app files will be refreshed; .env.local and instance/ are preserved."
fi

step "Creating directories"
run_step "create instance/logs/archives dirs" \
    mkdir -p "$INSTALL_DIR"/{instance,logs,archives,static/logos}
chmod 0750 "$INSTALL_DIR/archives" 2>/dev/null || true

step "Deploying files"
run_step "copy application files" cp -r \
      app.py healthcheck.py input_filter.py constants.py config.yaml requirements.txt \
      statuspage/ templates/ static/ tests/ docs/ start.sh stop.sh restart.sh rebuild.sh cleanup.sh install.sh README.md scripts/ lib.sh \
      "$INSTALL_DIR/"
if [ -d "$ROOT_DIR/vendor" ]; then
    cp -r "$ROOT_DIR/vendor" "$INSTALL_DIR/" 2>/dev/null || true
fi
chmod +x "$INSTALL_DIR"/*.sh "$INSTALL_DIR"/scripts/*.sh 2>/dev/null || true

# ── Customer logos (optional) ────────────────────────────────────
# Copy any repo-level logos/ directory into the deploy so a fresh clone
# keeps branding. Configure via config.yaml `logo: {path: ...}`.
if [ -d "$ROOT_DIR/logos" ] && [ -n "$(ls -A "$ROOT_DIR/logos" 2>/dev/null)" ]; then
    run_step "copy customer logos" \
        cp -r "$ROOT_DIR/logos/." "$INSTALL_DIR/static/logos/" 2>/dev/null || true
    echo "Copied customer logos from $ROOT_DIR/logos -> static/logos"
fi

if [ "$ROOTMODE" -eq 1 ]; then
    chown -R "$SERVICE_USER":"$SERVICE_USER" \
        "$INSTALL_DIR"/{instance,logs,archives,config.yaml} 2>/dev/null || true
else
    chmod -R u+rwX "$INSTALL_DIR"
fi

step "Python virtual environment"
VENV_DIR="$INSTALL_DIR/.venv"
PY="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"

if [ ! -x "$PY" ]; then
    if [ -d "$VENV_DIR" ]; then
        warn "Existing venv is broken (no python binary) — recreating."
        rm -rf "$VENV_DIR"
    fi
    run_step "create virtualenv" python3 -m venv "$VENV_DIR"
fi

# Ensure pip is available inside the venv (handles macOS/Linux distros where venv lacks pip)
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    if "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then
        ok "pip installed via ensurepip"
    fi
fi

# Try upgrading pip quietly (ignore network 403 / offline errors)
"$PY" -m pip install --upgrade pip --quiet 2>/dev/null || true

# Install dependencies (use vendor/ wheels first with --no-index to prevent 403 PyPI network blocks)
install_deps_install() {
    if [ -d "$INSTALL_DIR/vendor" ] && [ -n "$(ls -A "$INSTALL_DIR/vendor"/*.whl 2>/dev/null)" ]; then
        "$PY" -m pip install --no-index --find-links "$INSTALL_DIR/vendor" -r "$INSTALL_DIR/requirements.txt" --quiet
    else
        "$PY" -m pip install -r "$INSTALL_DIR/requirements.txt" --quiet
    fi
}
run_step "install requirements.txt" install_deps_install
"$PY" -c "import flask, gunicorn, werkzeug, yaml" 2>/dev/null \
    || die "Dependency verification failed." \
           "The venv exists but key packages are missing. Delete '$VENV_DIR' and re-run."
if [ "$ROOTMODE" -eq 0 ]; then
    chmod -R u+rwX "$VENV_DIR" 2>/dev/null || true
fi

# ── Validate numeric options before use ──────────────────────────
case "$STATUS_PORT" in
    ''|*[!0-9]*) die "Invalid port: $STATUS_PORT" "Port must be an integer 1-65535." ;;
esac
if [ "$STATUS_PORT" -lt 1 ] || [ "$STATUS_PORT" -gt 65535 ]; then
    die "Port out of range: $STATUS_PORT" "Port must be 1-65535."
fi
case "$GUNICORN_WORKERS" in
    ''|*[!0-9]*) die "Invalid worker count: $GUNICORN_WORKERS" "Workers must be a positive integer." ;;
esac
[ "$GUNICORN_WORKERS" -ge 1 ] || die "Worker count must be >= 1."

# ── Admin credentials ────────────────────────────────────────────
ENV_FILE="$INSTALL_DIR/.env.local"

creds_exist() { [ -f "$ENV_FILE" ] && grep -q '^STATUS_ADMIN_PASS_HASH=..*' "$ENV_FILE"; }

if [ "$UPGRADE" -eq 1 ] && creds_exist && [ "$FORCE_ENV" != "1" ]; then
    step "Admin credentials"
    ok "Existing $ENV_FILE kept (upgrade mode). Pass --force-env to replace."
    ADMIN_USER=$("$PY" - << 'PYEOF'
import yaml, os
cfg = yaml.safe_load(open(os.environ['INSTALL_DIR'] + '/config.yaml')) or {}
print((cfg.get('admin') or {}).get('user') or 'admin')
PYEOF
    ) || ADMIN_USER="admin"
elif [ "$NONINTERACTIVE" -eq 1 ]; then
    step "Admin credentials (non-interactive)"
    [ -n "$ADMIN_USER" ] || die "--admin-user is required in non-interactive mode." \
        "Pass --admin-user and --admin-pass (or set SP_ADMIN_USER / SP_ADMIN_PASS)."
    if [ ${#ADMIN_PASS} -lt 8 ]; then
        warn "Password is shorter than 8 characters — consider a stronger one."
    fi
    case "$ADMIN_PASS" in
        *$'\n'*|*$'\t'*) die "Password contains newline/tab characters." \
                         "Supply the password via SP_ADMIN_PASS without control chars." ;;
    esac
else
    step "Admin credentials"
    # Prompted FIRST: init_db() seeds the DB via the app, which requires
    # STATUS_ADMIN_PASS_HASH to be set — seeding before the env file exists
    # would abort the install (set -e) before the user is ever asked.
    read -rp "Admin username [$ADMIN_USER]: " ADMIN_USER_INPUT
    ADMIN_USER="${ADMIN_USER_INPUT:-$ADMIN_USER}"
    read -rsp "Admin password: " ADMIN_PASS_INPUT
    echo ""
    if [ -z "$ADMIN_PASS_INPUT" ]; then
        die "Admin password must not be empty." "Re-run and enter a password."
    fi
    if [ ${#ADMIN_PASS_INPUT} -lt 8 ]; then
        warn "Password is shorter than 8 characters — consider a stronger one."
    fi
    if printf '%s' "$ADMIN_PASS_INPUT" | grep -q "$(printf '\t')"; then
        die "Password contains a tab character (terminal echo artefact)." \
            "Re-run the install."
    fi
    ADMIN_PASS="$ADMIN_PASS_INPUT"
fi

step "Hashing credentials"
if ! "$PY" -c "import werkzeug.security" 2>/dev/null; then
    warn "werkzeug not found in $VENV_DIR — installing requirements..."
    if [ -d "$INSTALL_DIR/vendor" ] && [ -n "$(ls -A "$INSTALL_DIR/vendor"/*.whl 2>/dev/null)" ]; then
        "$PY" -m pip install --no-index --find-links "$INSTALL_DIR/vendor" werkzeug --quiet 2>/dev/null || true
    else
        "$PY" -m pip install werkzeug --quiet 2>/dev/null || true
    fi
fi

export _SP_PASS="$ADMIN_PASS"
PASS_HASH="$("$PY" - << 'PYEOF'
import os, sys, hashlib, secrets
pwd = os.environ.get('_SP_PASS', '')
try:
    from werkzeug.security import generate_password_hash
    print(generate_password_hash(pwd))
except Exception:
    salt = secrets.token_hex(16)
    if hasattr(hashlib, 'scrypt'):
        try:
            h = hashlib.scrypt(pwd.encode('utf-8'), salt=salt.encode('utf-8'), n=32768, r=8, p=1, maxmem=64*1024*1024).hex()
            print('scrypt:32768:8:1$' + salt + '$' + h)
            sys.exit(0)
        except Exception:
            pass
    h = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'), salt.encode('utf-8'), 1000000).hex()
    print('pbkdf2:sha256:1000000$' + salt + '$' + h)
PYEOF
)"
unset _SP_PASS
[ -n "$PASS_HASH" ] || die "Could not hash the password." \
       "Check that requirements.txt installed cleanly: $PIP install -r $INSTALL_DIR/requirements.txt"

SECRET_KEY="$("$PY" -c "import secrets; print(secrets.token_hex(32))")"

sync_admin_user_to_config() {
    export _SP_INSTALL_USER="$ADMIN_USER"
    "$PY" - << 'PYEOF' || die "Failed to write admin username into config.yaml."
import yaml, os
p = os.environ['INSTALL_DIR'] + '/config.yaml'
cfg = yaml.safe_load(open(p)) or {}
cfg['admin'] = cfg.get('admin', {})
cfg['admin']['user'] = os.environ['_SP_INSTALL_USER']
open(p, 'w').write(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
PYEOF
}

write_env_file() {
    {
        printf "%s='%s'\n" "STATUS_ADMIN_PASS_HASH" "$PASS_HASH"
        printf "%s='%s'\n" "STATUS_SECRET_KEY" "$SECRET_KEY"
        printf 'PYTHONUNBUFFERED=1\n'
    } > "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"
}

step "Environment file"
mkdir -p "$(dirname "$ENV_FILE")"

if [ "$UPGRADE" -eq 1 ] && creds_exist && [ "$FORCE_ENV" != "1" ]; then
    : # already handled above — keep existing file byte-for-byte
elif [ -f "$ENV_FILE" ] && [ "$FORCE_ENV" != "1" ]; then
    # Existing credentials in the install dir: keep them, but tell the user —
    # the supplied values above are NOT applied unless override is explicit.
    warn "$ENV_FILE already exists — keeping existing credentials."
    echo "    The supplied password was ignored. Remove $ENV_FILE to re-create it,"
    echo "    or re-run with --force-env to force the new credentials."
    sync_admin_user_to_config
elif [ -f "$ROOT_DIR/.env.local" ] && [ "$FORCE_ENV" != "1" ] \
     && grep -q '^STATUS_ADMIN_PASS_HASH=..*' "$ROOT_DIR/.env.local" 2>/dev/null; then
    # Project env (e.g. a dev-only password / healthchecks-disabled flag) would
    # silently override the credentials just supplied — only reuse when the
    # source env actually exports a real admin hash.
    warn "Reusing STATUS_ADMIN_PASS_HASH from $ROOT_DIR/.env.local —"
    echo "    the supplied password is NOT used. The new secret key is applied."
    { grep -v '^STATUS_SECRET_KEY=' "$ROOT_DIR/.env.local" || true; } > "$ENV_FILE.tmp"
    printf "%s='%s'\n" "STATUS_SECRET_KEY" "$SECRET_KEY" >> "$ENV_FILE.tmp"
    if ! grep -q '^PYTHONUNBUFFERED=' "$ENV_FILE.tmp"; then
        printf 'PYTHONUNBUFFERED=1\n' >> "$ENV_FILE.tmp"
    fi
    mv "$ENV_FILE.tmp" "$ENV_FILE"
    sync_admin_user_to_config
    echo "Credentials set: user=$ADMIN_USER (password from $ROOT_DIR/.env.local)"
else
    sync_admin_user_to_config
    write_env_file
    echo "Credentials set: user=$ADMIN_USER (new password)"
fi
chmod 0600 "$ENV_FILE"
ok "Env file ready at $ENV_FILE"

# Post-write sanity check (authoritative): parse the file via dotenv exactly
# like app.py does, and confirm the scrypt hash survived intact ($ separators
# present). Catches any quoting corruption before it breaks logins.
export INSTALL_DIR
"$PY" - << 'PYEOF' || die ".env file failed the post-write sanity check." \
    "This means the hash would be truncated at runtime. Report this bug."
import os
from dotenv import dotenv_values
vals = dotenv_values(os.environ["INSTALL_DIR"] + "/.env.local")
h = vals.get("STATUS_ADMIN_PASS_HASH", "")
assert h, "STATUS_ADMIN_PASS_HASH missing/empty after dotenv parse"
assert "$" in h, "hash appears truncated (no $ separators)"
PYEOF

step "Initialize database"
cd "$INSTALL_DIR"
if ! run_step "seed database (init_db)" \
    env STATUS_ADMIN_PASS_HASH="$PASS_HASH" STATUS_NO_ARCHIVE="${UPGRADE:+1}" \
    "$PY" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from app import init_db; init_db()"; then
    : # unreachable under set -e; kept for clarity
fi

if [ "$ROOTMODE" -eq 1 ]; then
    step "Installing systemd service"
    cat > "/etc/systemd/system/$SYSTEMD_NAME" << SVCEOF
[Unit]
Description=Status Page Web App
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin"
ExecStart=$VENV_DIR/bin/gunicorn --bind $BIND_HOST:$STATUS_PORT --workers $GUNICORN_WORKERS --timeout 30 app:app
Restart=on-failure
RestartSec=5

# Hardening: the app only needs its own directories.
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$INSTALL_DIR/instance $INSTALL_DIR/logs $INSTALL_DIR/archives $INSTALL_DIR/static

[Install]
WantedBy=multi-user.target
SVCEOF
    run_step "systemd daemon-reload" systemctl daemon-reload
    run_step "enable service" systemctl enable "$SYSTEMD_NAME"
    if run_step "start service" systemctl start "$SYSTEMD_NAME"; then
        # Give it a moment, then verify it actually serves on the configured port.
        sleep 2
        if curl -fsS -o /dev/null "http://127.0.0.1:${STATUS_PORT}/" 2>/dev/null; then
            ok "Service is up and serving on port $STATUS_PORT."
        else
            warn "Service started but http://127.0.0.1:$STATUS_PORT/ did not respond within 2s."
            echo "    Check: journalctl -u $SYSTEMD_NAME -n 50"
        fi
    fi
else
    # Keep start.sh's bind target in sync so manual starts match this install.
    if ! grep -q -- "--bind $BIND_HOST:$STATUS_PORT" "$INSTALL_DIR/start.sh" 2>/dev/null; then
        "$PY" -c "
import sys, re
path, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, 'r') as f:
        content = f.read()
    content = re.sub(r'gunicorn --bind [^ ]*', f'gunicorn --bind {host}:{port}', content)
    with open(path, 'w') as f:
        f.write(content)
except Exception:
    pass
" "$INSTALL_DIR/start.sh" "$BIND_HOST" "$STATUS_PORT" 2>/dev/null || true
    fi
    echo ""
    echo "Non-root install complete. Start manually:"
    echo "  cd $INSTALL_DIR && ./start.sh"
    echo "Serving on http://$BIND_HOST:$STATUS_PORT"
fi
echo ""

echo "========================================"
echo "  Deployment complete!"
echo "  Install dir: $INSTALL_DIR"
echo "  Admin user:  $ADMIN_USER"
echo "  Address:     http://$BIND_HOST:$STATUS_PORT"
echo "  Logo (optional): ./scripts/install_logo.sh /path/to/logo.png $INSTALL_DIR"
echo "========================================"
