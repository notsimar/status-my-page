#!/usr/bin/env bash
# dev-setup.sh — Developer quick-start for status-my-page
# Creates a local venv, installs deps, seeds DB, and runs Flask dev server.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
ENV_FILE="$ROOT_DIR/.env.local"

echo "=== status-my-page developer setup ==="
echo "Root: $ROOT_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installing requirements..."
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt" >/dev/null

echo "Initializing database..."
STATUS_DISABLE_HEALTHCHECKS=1 "$VENV_DIR/bin/python3" -c "
import sys
sys.path.insert(0, '$ROOT_DIR')
from app import init_db
init_db()
print('DB initialized')
"

if [ ! -f "$ENV_FILE" ]; then
    echo "Generating development env..."
    # Generate env values using temporary Python scripts to avoid shell quoting issues
    PASS_HASH=$("$VENV_DIR/bin/python3" /dev/stdin << 'PYEOF'
import sys
sys.path.insert(0, '$ROOT_DIR')
from werkzeug.security import generate_password_hash
print(generate_password_hash("devpassword"))
PYEOF
)
    SECRET_KEY=$("$VENV_DIR/bin/python3" /dev/stdin << 'PYEOF'
import sys
sys.path.insert(0, '$ROOT_DIR')
import secrets
print(secrets.token_hex(32))
PYEOF
)
    # Write env file
    printf 'STATUS_DISABLE_HEALTHCHECKS=1\nSTATUS_ADMIN_PASS_HASH=%s\nSTATUS_SECRET_KEY=%s\n' \
        "$PASS_HASH" "$SECRET_KEY" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi

# Export env vars from .env.local so the Flask dev server can use them
export STATUS_DISABLE_HEALTHCHECKS=1

# Shell-source the .env.local file to ensure vars are in the environment
if [ -f "$ENV_FILE" ]; then
    # Read and export STATUS_ variables from the env file
    while IFS= read -r line; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue
        # Extract key and value
        key="${line%%=*}"
        value="${line#*=}"
        # Trim whitespace
        key=$(echo "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        value=$(echo "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        # Only export STATUS_ variables
        if [[ "$key" == STATUS_* ]]; then
            export "$key=$value"
        fi
    done < "$ENV_FILE"
fi

echo ""
echo "Setup complete!"
echo ""
echo "To start dev server:"
echo "  source $VENV_DIR/bin/activate"
echo "  flask --app app run --host 0.0.0.0 --port 8920"
echo ""
echo "Admin user: admin"
echo "Admin password: devpassword (change in $ENV_FILE or config)"