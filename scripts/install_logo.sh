#!/usr/bin/env bash
# install_logo.sh — Install a customer logo into a status-my-page deployment.
#
# Copies logo file(s) into <install_dir>/static/logos/ and writes the
# `logo:` section of config.yaml so the page renders it.
#
# Usage:
#   ./install_logo.sh /path/to/logo.png [INSTALL_DIR]        # single logo
#   LOGO_DARK=dark.png LOGO_LIGHT=light.png ./install_logo.sh [INSTALL_DIR]
#
# Single logo: $1 is the file (copied to static/logos/logo.<ext>).
# Dual logos:  LOGO_DARK / LOGO_LIGHT env vars; files are copied to
#              dark-logo.png / light-logo.png. config.yaml points at
#              whichever LOGO_CONFIG_PATH names (default light-logo.png).

set -euo pipefail

DEFAULT_INSTALL_DIR="$HOME/.local/share/status-page"
LOGO_CONFIG_PATH="${LOGO_CONFIG_PATH:-logos/light-logo.png}"

ext_of() { local f="$1"; [ "${f##*.}" != "$f" ] && echo ".${f##*.}" || echo ""; }

copy_logo() {
    local src="$1" dest_name="$2"
    case "$src" in
        /*) : ;;
        *) src="$(pwd)/$src" ;;
    esac
    if [ ! -f "$src" ]; then
        echo "ERROR: Logo file not found: $src"
        exit 1
    fi
    mkdir -p "$(dirname "$INSTALL_DIR/static/logos/$dest_name")"
    cp "$src" "$INSTALL_DIR/static/logos/$dest_name"
    chmod 0644 "$INSTALL_DIR/static/logos/$dest_name"
    echo "Installed: $src -> static/logos/$dest_name"
}

# ── Argument parsing ─────────────────────────────────────────────
DUAL=0
if [ -n "${LOGO_DARK:-}" ] || [ -n "${LOGO_LIGHT:-}" ]; then
    DUAL=1
    INSTALL_DIR="${1:-$DEFAULT_INSTALL_DIR}"
else
    if [ $# -lt 1 ]; then
        echo "Usage: $0 /path/to/logo.png [INSTALL_DIR]"
        echo "   or: LOGO_DARK=f.png LOGO_LIGHT=f.png $0 [INSTALL_DIR]"
        exit 1
    fi
    LOGO_FILE="$1"
    INSTALL_DIR="${2:-$DEFAULT_INSTALL_DIR}"
fi

case "$INSTALL_DIR" in
    /*) : ;;
    *) echo "ERROR: Install path must be absolute: $INSTALL_DIR"; exit 1 ;;
esac
if command -v python3 &>/dev/null; then
    INSTALL_DIR=$(python3 -c "import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))" "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")
elif command -v realpath &>/dev/null; then
    INSTALL_DIR=$(realpath -m "$INSTALL_DIR" 2>/dev/null || realpath "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")
fi
CONFIG_YAML="$INSTALL_DIR/config.yaml"

if [ ! -f "$CONFIG_YAML" ]; then
    echo "ERROR: No config.yaml in $INSTALL_DIR — is this a status-my-page install?"
    exit 1
fi

# ── Copy logo files ──────────────────────────────────────────────
if [ "$DUAL" -eq 0 ]; then
    copy_logo "$LOGO_FILE" "logo$(ext_of "$LOGO_FILE")"
else
    [ -n "${LOGO_DARK:-}" ] && copy_logo "$LOGO_DARK" "dark-logo.png"
    [ -n "${LOGO_LIGHT:-}" ] && copy_logo "$LOGO_LIGHT" "light-logo.png"
fi

# ── Update config.yaml logo section ──────────────────────────────
python3 - "$CONFIG_YAML" "$LOGO_CONFIG_PATH" << 'PYEOF'
import os
import sys

import yaml

config_path, logo_path = sys.argv[1], sys.argv[2]

cfg = {}
if os.path.exists(config_path) and os.path.getsize(config_path) > 0:
    cfg = yaml.safe_load(open(config_path)) or {}
if not isinstance(cfg, dict):
    cfg = {}
if not isinstance(cfg.get("logo"), dict):
    cfg["logo"] = {}
cfg["logo"]["path"] = logo_path

tmp = config_path + ".tmp"
with open(tmp, "w") as f:
    yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
os.replace(tmp, config_path)
print(f"config.yaml updated: logo.path = {logo_path}")
PYEOF

echo ""
echo "Logo installation complete."
[ -x "$INSTALL_DIR/restart.sh" ] \
    && echo "Restart the server to see it: cd $INSTALL_DIR && ./restart.sh" \
    || true
