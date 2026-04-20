#!/usr/bin/env bash
# Install llm-git-guard: provision host directories, build the container
# image, and bring up the service with docker compose.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "install.sh: must run as root (try: sudo $0 $*)" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/llm-git-guard
DATA_DIR=/var/lib/llm-git-guard
CONFIG_DIR=/etc/llm-git-guard

if ! command -v docker >/dev/null 2>&1; then
    echo "install.sh: docker not found in PATH" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "install.sh: 'docker compose' plugin not available" >&2
    exit 1
fi

echo "==> project files -> $PREFIX"
install -d -m 755 "$PREFIX"
# tar-pipe keeps us portable when rsync isn't installed (common on slim hosts).
tar -C "$REPO_ROOT" \
    --exclude=.git --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=.venv --exclude=.mypy_cache --exclude=.pytest_cache \
    -cf - . | tar -C "$PREFIX" -xf -

echo "==> data dir -> $DATA_DIR"
install -d -m 755 "$DATA_DIR/repos"

echo "==> config dir -> $CONFIG_DIR"
install -d -m 755 "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/exempt-repos.txt" ]]; then
    install -m 644 "$REPO_ROOT/config/exempt-repos.txt" "$CONFIG_DIR/exempt-repos.txt"
else
    echo "   (leaving existing $CONFIG_DIR/exempt-repos.txt untouched)"
fi

echo "==> SSH key sanity check"
if [[ ! -f /root/.ssh/id_ed25519 ]]; then
    echo "   WARNING: /root/.ssh/id_ed25519 missing -- upstream pushes will fail"
fi
if [[ ! -f /root/.ssh/known_hosts ]]; then
    echo "   WARNING: /root/.ssh/known_hosts missing -- StrictHostKeyChecking may fail"
fi

echo "==> docker compose build + up"
cd "$PREFIX"
docker compose build
docker compose up -d
docker compose ps

cat <<EOF

==> installed.

Next steps:
  1. From your normal user, repoint existing clones:
         $PREFIX/scripts/rewrite-origins.sh
  2. Swap gh-sync so new clones come through llm-git-guard:
         ln -sf $PREFIX/scripts/gh-sync ~/.local/bin/gh-sync
  3. Make sure /root/.ssh/id_ed25519 is your GitHub key and your user no
     longer has a copy.
EOF
