#!/usr/bin/env bash
# Install llm-git-guard system-wide: place sources under /opt, data under
# /var/lib, config under /etc, then enable the systemd service.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "install.sh: must run as root (try: sudo $0 $*)" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/llm-git-guard
DATA_DIR=/var/lib/llm-git-guard
CONFIG_DIR=/etc/llm-git-guard
SYSTEMD_UNIT=/etc/systemd/system/llm-git-guard.service

echo "==> files -> $PREFIX"
install -d -m 755 "$PREFIX/src/llm_git_guard" "$PREFIX/hooks" "$PREFIX/scripts"
install -m 755 "$REPO_ROOT/src/llm_git_guard/server.py"    "$PREFIX/src/llm_git_guard/server.py"
install -m 644 "$REPO_ROOT/src/llm_git_guard/__init__.py"  "$PREFIX/src/llm_git_guard/__init__.py"
install -m 755 "$REPO_ROOT/hooks/pre-receive"              "$PREFIX/hooks/pre-receive"
install -m 755 "$REPO_ROOT/scripts/rewrite-origins.sh"     "$PREFIX/scripts/rewrite-origins.sh"
install -m 755 "$REPO_ROOT/scripts/gh-sync"                "$PREFIX/scripts/gh-sync"

echo "==> data dir -> $DATA_DIR"
install -d -m 755 "$DATA_DIR/repos"

echo "==> config dir -> $CONFIG_DIR"
install -d -m 755 "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/exempt-repos.txt" ]]; then
    install -m 644 "$REPO_ROOT/config/exempt-repos.txt" "$CONFIG_DIR/exempt-repos.txt"
else
    echo "   (leaving existing $CONFIG_DIR/exempt-repos.txt untouched)"
fi

echo "==> systemd unit -> $SYSTEMD_UNIT"
install -m 644 "$REPO_ROOT/systemd/llm-git-guard.service" "$SYSTEMD_UNIT"
systemctl daemon-reload
systemctl enable --now llm-git-guard.service
systemctl --no-pager --full status llm-git-guard.service || true

cat <<EOF

==> installed. what's next:

  1. confirm /root/.ssh/id_ed25519 is registered with GitHub and that
     /root/.ssh/known_hosts contains github.com.
  2. as your normal user, repoint existing clones to the proxy:
         $PREFIX/scripts/rewrite-origins.sh
  3. replace ~/.local/bin/gh-sync with the proxy-aware version so future
     clones come through llm-git-guard from the start:
         ln -sf $PREFIX/scripts/gh-sync ~/.local/bin/gh-sync
  4. remove the SSH key from your user account so direct git pushes are
     impossible; only root (and therefore the proxy) has it.
EOF
