#!/usr/bin/env bash
# build_release.sh — Build a clean, deployable tar.gz of status-my-page.
#
# Produces: dist/status-my-page-<version>.tar.gz
#   containing only git-tracked files (no venv, logs, env files, QA output),
#   extracted into status-my-page/ so `tar -xzf ... && cd status-my-page &&
#   ./install.sh` just works.
#
# The version is taken from the latest git tag (falling back to short SHA).
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
if [ -z "$VERSION" ]; then
    VERSION="0.0.0-$(git rev-parse --short HEAD)"
fi
NAME="status-my-page-${VERSION}"

OUT_DIR="dist"
TARBALL="$OUT_DIR/${NAME}.tar.gz"

echo "=== Building release: $NAME ==="

rm -rf "$OUT_DIR/$NAME"
mkdir -p "$OUT_DIR/$NAME"

# Copy exactly what git tracks — nothing untracked leaks in.
git archive --format=tar HEAD | tar -x -C "$OUT_DIR/$NAME"

mkdir -p "$OUT_DIR"
tar -czf "$TARBALL" -C "$OUT_DIR" "$NAME"
rm -rf "$OUT_DIR/$NAME"

SIZE=$(du -h "$TARBALL" | cut -f1)
FILES=$(tar -tzf "$TARBALL" | grep -vc '/$')

echo ""
echo "=== Release built ==="
echo "  $TARBALL ($SIZE, $FILES files)"
echo ""
echo "Deploy:"
echo "  scp $TARBALL user@host:"
echo "  ssh user@host 'tar xzf ${NAME}.tar.gz && cd $NAME && ./install.sh'"
