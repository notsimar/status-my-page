#!/usr/bin/env bash
# install.sh — Deploy status page as an unprivileged user (non-root by default)
# Run as the target user (no sudo required):
#   cd status-page && ./install.sh [INSTALL_DIR]
# Default INSTALL_DIR = $HOME/.local/share/status-page
set -euo pipefail

# Determine the project root directory (where this script lives)
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Allow override via environment variable for testing/flexibility
ROOT_DIR="${STATUS_MY_PAGE_ROOT:-$ROOT_DIR}"

# Shared error-reporting helpers (die/warn/step/ok/run_step/require_cmd)
source "$ROOT_DIR/lib.sh"

INSTALL_DIR="${1:-$HOME/.local/share/status-page}"
SERVICE_USER="$(whoami)"
SYSTEMD_NAME="status-page.service"

step "Validating installation path"
case "$INSTALL_DIR" in
    /*) : ;;
    *) die "Install path must be absolute: $INSTALL_DIR" \
           "Pass a full path, e.g. $HOME/.local/share/status-page" ;;
esac
if echo "$INSTALL_DIR" | grep -q '\.\./'; then
    die "Invalid install path (traversal not allowed): $INSTALL_DIR" \
        "Remove any '..' segments from the path."
fi
INSTALL_DIR=$(realpath -m "$INSTALL_DIR")
export INSTALL_DIR
ok "Install directory: $INSTALL_DIR"
echo "Service user: $SERVICE_USER"

step "Preflight checks"
require_cmd python3 "Install python3 + python3-venv first (e.g. sudo dnf install python3)."
PYTHON_VER=$(python3 --version | awk '{print $2}' | cut -d. -f1,2)
ok "Python version: $PYTHON_VER"

if ! command -v realpath &>/dev/null; then
    die "Required command 'realpath' not found." \
        "It is part of coreutils; update your system packages."
fi

# Non-root: skip system package installs
if [ "$(id -u)" -eq 0 ]; then
    warn "Running as root. Will attempt system installs and ownership changes."
    ROOT=1
else
    ok "Running as non-root user $SERVICE_USER. Skipping system packages, user creation, systemd."
    ROOT=0
fi

step "Creating directories"
run_step "create instance/logs/archives dirs" \
    mkdir -p "$INSTALL_DIR"/{instance,logs,archives}
chmod 0750 "$INSTALL_DIR/archives" 2>/dev/null || true

step "Deploying files"
run_step "copy application files" cp -r \
      app.py healthcheck.py input_filter.py constants.py config.yaml requirements.txt \
      statuspage/ templates/ static/ tests/ docs/ start.sh stop.sh restart.sh rebuild.sh cleanup.sh install.sh README.md scripts/ lib.sh \
      "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR"/*.sh "$INSTALL_DIR"/scripts/*.sh 2>/dev/null || true

# ── Customer logos (optional) ────────────────────────────────────
# Copy any repo-level logos/ directory into the deploy so a fresh clone
# keeps branding. Configure via config.yaml `logo: {path: ...}`.
if [ -d "$ROOT_DIR/logos" ] && [ -n "$(ls -A "$ROOT_DIR/logos" 2>/dev/null)" ]; then
    run_step "copy customer logos" \
        cp -r "$ROOT_DIR/logos/." "$INSTALL_DIR/static/logos/" 2>/dev/null || true
    echo "Copied customer logos from $ROOT_DIR/logos -> static/logos"
fi

if [ "$ROOT" -eq 1 ]; then
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"/{instance,logs,archives,config.yaml} 2>/dev/null || true
else
    chmod -R u+rwX "$INSTALL_DIR"
fi

step "Python virtual environment"
VENV_DIR="$INSTALL_DIR/.venv"
PY="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"

if [ ! -d "$VENV_DIR" ]; then
    run_step "create virtualenv" python3 -m venv "$VENV_DIR"
fi
run_step "upgrade pip" "$PIP" install --upgrade pip --quiet
if ! run_step "install requirements.txt" "$PIP" install -r "$INSTALL_DIR/requirements.txt" --quiet; then
    : # run_step already reported; unreachable due to exit, kept for clarity
fi
"$PY" -c "import flask, gunicorn, werkzeug, yaml" 2>/dev/null \
    || die "Dependency verification failed." \
           "The venv exists but key packages are missing. Delete '$VENV_DIR' and re-run."
if [ "$ROOT" -eq 0 ]; then
    chmod -R u+rwX "$VENV_DIR" 2>/dev/null || true
fi

# ── Admin credentials ────────────────────────────────────────────
# Prompted FIRST: init_db() seeds the DB via the app, which requires
# STATUS_ADMIN_PASS_HASH to be set — seeding before the env file exists
# would abort the install (set -e) before the user is ever asked.
step "Admin credentials"
read -rp "Admin username [admin]: " ADMIN_USER_INPUT
ADMIN_USER="${ADMIN_USER_INPUT:-admin}"
read -rsp "Admin password: " ADMIN_PASS_INPUT
echo ""
if [ -z "$ADMIN_PASS_INPUT" ]; then
    die "Admin password must not be empty." "Re-run and enter a password."
fi
if [ ${#ADMIN_PASS_INPUT} -lt 8 ]; then
    warn "Password is shorter than 8 characters — consider a stronger one."
fi
if [ -t 1 ] && printf '%s' "$ADMIN_PASS_INPUT" | grep -q $'\t'; then
    die "Password contains a tab character (terminal echo artefact)." \
        "Re-run the install."
fi

step "Hashing credentials"
PASS_HASH="$("$PY" -c "from werkzeug.security import generate_password_hash; import sys; pwd=sys.stdin.read(); print(generate_password_hash(pwd.rstrip('\n')))" <<< "$ADMIN_PASS_INPUT")" \
    || die "Could not hash the password (werkzeug import failed?)." \
           "Check that requirements.txt installed cleanly: $PIP install -r $INSTALL_DIR/requirements.txt"
[ -n "$PASS_HASH" ] || die "Password hashing produced an empty result."

SECRET_KEY="$("$PY" -c "import secrets; print(secrets.token_hex(32))")"
ENV_FILE="$INSTALL_DIR/.env.local"
mkdir -p "$(dirname "$ENV_FILE")"

write_env_file() {
    {
        printf "%s='%s'\n" "STATUS_ADMIN_PASS_HASH" "$PASS_HASH"
        printf "%s='%s'\n" "STATUS_SECRET_KEY" "$SECRET_KEY"
        printf 'PYTHONUNBUFFERED=1\n'
    } > "$ENV_FILE"
}

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

if [ -f "$ENV_FILE" ]; then
    # Existing credentials in the install dir: keep them, but tell the user —
    # the prompted values above are NOT applied unless override is explicit.
    warn "$ENV_FILE already exists — keeping existing credentials."
    echo "    The prompted password was ignored. Remove $ENV_FILE to re-create it,"
    echo "    or re-run with SP_INSTALL_OVERRIDE_ENV=1 to force the new credentials."
    if [ "${SP_INSTALL_OVERRIDE_ENV:-0}" = "1" ]; then
        echo "    Override active — replacing $ENV_FILE with prompted credentials."
        write_env_file
        sync_admin_user_to_config
        echo "    Credentials set: user=$ADMIN_USER (new password)"
    else
        echo "    Keeping existing $ENV_FILE untouched."
    fi
elif [ -f "$ROOT_DIR/.env.local" ]; then
    # Project env (e.g. a dev-only password / healthchecks-disabled flag) would
    # silently override the credentials just prompted — only reuse when the
    # source env actually exports a real admin hash.
    if grep -q '^STATUS_ADMIN_PASS_HASH=..*' "$ROOT_DIR/.env.local" 2>/dev/null; then
        warn "Reusing STATUS_ADMIN_PASS_HASH from $ROOT_DIR/.env.local —"
        echo "    the prompted password is NOT used. The new secret key is applied."
        { grep -v '^STATUS_SECRET_KEY=' "$ROOT_DIR/.env.local" || true; } > "$ENV_FILE.tmp"
        printf "%s='%s'\n" "STATUS_SECRET_KEY" "$SECRET_KEY" >> "$ENV_FILE.tmp"
        if ! grep -q '^PYTHONUNBUFFERED=' "$ENV_FILE.tmp"; then
            printf 'PYTHONUNBUFFERED=1\n' >> "$ENV_FILE.tmp"
        fi
        mv "$ENV_FILE.tmp" "$ENV_FILE"
        # Keep the admin username in sync with the prompt
        sync_admin_user_to_config
        echo "Credentials set: user=$ADMIN_USER (password from $ROOT_DIR/.env.local)"
    else
        # Source env has no usable hash — fall through to fresh creation.
        echo "Source $ROOT_DIR/.env.local has no STATUS_ADMIN_PASS_HASH — creating a fresh env file."
        write_env_file
        echo "Credentials set: user=$ADMIN_USER (new password)"
    fi
else
    # Fresh install: write prompted credentials
    sync_admin_user_to_config
    write_env_file
    echo "Credentials set: user=$ADMIN_USER (new password)"
fi
chmod 0600 "$ENV_FILE"
ok "Env file created at $ENV_FILE"

# Sanity check: source the file and confirm the vars resolve to real values.
if ! bash -c "set -a; source '$ENV_FILE'; set +a; [[ -n \${STATUS_ADMIN_PASS_HASH:-} && \$STATUS_ADMIN_PASS_HASH == *'*'* ]] && false" 2>/dev/null; then
    : # quoting style differs; do a functional check instead:
    if ! bash -c "set -a; source '$ENV_FILE'; set +a; \"\$INSTALL_VENV_PY\" -c pass" 2>/dev/null; then :; fi
fi
bash -c "
set -a
source '$ENV_FILE'
set +a
[ -n \"\${STATUS_ADMIN_PASS_HASH:-}\" ] || { echo 'STATUS_ADMIN_PASS_HASH empty after source' >&2; exit 1; }
case \"\$STATUS_ADMIN_PASS_HASH\" in
    *'$'*) ;;   # contains at least one dollar → full scrypt hash survived
    *) echo 'WARNING: hash appears truncated (no \$ separators) after sourcing.' >&2; exit 1 ;;
esac
" || die ".env file failed the post-write sanity check." \
         "This means the hash would be truncated at runtime. Report this bug."

step "Initialize database"
cd "$INSTALL_DIR"
run_step "seed database (init_db)" "$PY" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from app import init_db; init_db()"

if [ "$ROOT" -eq 1 ]; then
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
ExecStart=$VENV_DIR/bin/gunicorn --bind 0.0.0.0:8920 --workers 2 --timeout 30 app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF
    run_step "systemd daemon-reload" systemctl daemon-reload
    run_step "enable service" systemctl enable "$SYSTEMD_NAME"
    if run_step "start service" systemctl start "$SYSTEMD_NAME"; then
        # Give it a moment, then verify it actually serves.
        sleep 2
        if ! curl -fsS -o /dev/null "http://127.0.0.1:${STATUS_PORT:-8920}/" 2>/dev/null; then
            warn "Service started but http://127.0.0.1:8920/ did not respond within 2s."
            echo "    Check: journalctl -u $SYSTEMD_NAME -n 50"
        else
            ok "Service is up and serving on port ${STATUS_PORT:-8920}."
        fi
    fi
else
    echo ""
    echo "Non-root install complete. Start manually:"
    echo "  cd $INSTALL_DIR && ./start.sh"
fi
echo ""

echo "========================================"
echo "  Deployment complete!"
echo "  Install dir: $INSTALL_DIR"
echo "  Admin user: $ADMIN_USER"
echo "  Logo (optional): ./scripts/install_logo.sh /path/to/logo.png $INSTALL_DIR"
echo "========================================"
