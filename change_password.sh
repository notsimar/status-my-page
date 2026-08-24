#!/usr/bin/env bash
# change_password.sh — Change admin password for status-my-page
# Generates werkzeug password hash and updates .env.local / .env file

# ── Help ───────────────────────────────────────
usage() {
    sed -n '2,5p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

case "${1:-}" in
    -h|--help) usage ;;
esac

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"
VENV_DIR="$ROOT_DIR/.venv"

# Prefer .venv python if available, else system python3
if [ -x "$VENV_DIR/bin/python3" ]; then
    PYTHON_BIN="$VENV_DIR/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "Error: python3 not found." >&2
    exit 1
fi

# Verify werkzeug is available
if ! "$PYTHON_BIN" -c "import werkzeug.security" >/dev/null 2>&1; then
    echo "Error: werkzeug is not installed in '$PYTHON_BIN'." >&2
    echo "Please activate your virtual environment or run ./dev-setup.sh first." >&2
    exit 1
fi

echo "=== status-my-page Admin Password Change ==="

# Read password securely
prompt_password() {
    local prompt_text="$1"
    local pass=""
    stty -echo 2>/dev/null || true
    read -r -p "$prompt_text" pass
    stty echo 2>/dev/null || true
    echo "" >&2
    printf "%s" "$pass"
}

while true; do
    PASS1=$(prompt_password "Enter new admin password: ")
    if [ -z "$PASS1" ]; then
        echo "Password cannot be empty. Please try again." >&2
        continue
    fi

    PASS2=$(prompt_password "Confirm new admin password: ")
    if [ "$PASS1" != "$PASS2" ]; then
        echo "Passwords do not match. Please try again." >&2
        continue
    fi
    break
done

# Generate werkzeug hash
export _SP_PASS="$PASS1"
NEW_HASH="$("$PYTHON_BIN" - << 'PYEOF'
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

# Target env file resolution (.env.local takes precedence if it exists, else .env)
if [ -f "$ROOT_DIR/.env.local" ]; then
    TARGET_ENV="$ROOT_DIR/.env.local"
elif [ -f "$ROOT_DIR/.env" ]; then
    TARGET_ENV="$ROOT_DIR/.env"
else
    TARGET_ENV="$ROOT_DIR/.env.local"
    touch "$TARGET_ENV"
    chmod 600 "$TARGET_ENV"
fi

# Update or insert STATUS_ADMIN_PASS_HASH in target env file safely
"$PYTHON_BIN" -c '
import sys

env_path = sys.argv[1]
new_hash = sys.argv[2]

lines = []
found = False

try:
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
except FileNotFoundError:
    lines = []

new_lines = []
for line in lines:
    if line.strip().startswith("STATUS_ADMIN_PASS_HASH="):
        new_lines.append(f"STATUS_ADMIN_PASS_HASH='{new_hash}'\n")
        found = True
    else:
        new_lines.append(line)

if not found:
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines.append("\n")
    new_lines.append(f"STATUS_ADMIN_PASS_HASH='{new_hash}'\n")

with open(env_path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)
' "$TARGET_ENV" "$NEW_HASH"

chmod 600 "$TARGET_ENV"

echo "✓ Admin password updated successfully in $TARGET_ENV"
if [ -f "$ROOT_DIR/.server.pid" ]; then
    echo "Note: If the server is currently running, restart it for changes to take effect."
fi
