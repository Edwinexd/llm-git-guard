#!/usr/bin/env bash
# Install the ssh-authd wrapper for a given user so that their `ssh`
# (and `scp`, `sftp`) go through the guardrail instead of their own
# SSH key. Idempotent.
#
# Usage:  sudo install-ssh-authd.sh <username>
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "install-ssh-authd.sh: must run as root" >&2
    exit 1
fi

USER_NAME="${1:-}"
if [[ -z "$USER_NAME" ]]; then
    echo "usage: sudo $0 <username>" >&2
    exit 2
fi
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
    echo "install-ssh-authd.sh: user '$USER_NAME' does not exist" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN=/usr/local/bin/ssh-authd
SBIN=/usr/local/sbin/ssh-authd-exec
SUDOERS=/etc/sudoers.d/llm-git-guard-ssh-authd

echo "==> installing ssh-authd binaries"
install -m 755 "$REPO_ROOT/scripts/ssh-authd"      "$BIN"
install -m 755 "$REPO_ROOT/scripts/ssh-authd-exec" "$SBIN"

echo "==> installing sudoers rule for $USER_NAME"
tmp=$(mktemp)
cat > "$tmp" <<EOF
# llm-git-guard: allow $USER_NAME to invoke the ssh-authd privileged helper.
# The helper runs /usr/bin/ssh with root's SSH key and refuses GitHub.
$USER_NAME ALL=(root) NOPASSWD: $SBIN
EOF
visudo -cf "$tmp" >/dev/null
install -m 440 -o root -g root "$tmp" "$SUDOERS"
rm -f "$tmp"

USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
USER_BIN="$USER_HOME/.local/bin"
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$USER_BIN"

echo "==> symlinking ssh/scp/sftp in $USER_BIN"
for name in ssh scp sftp; do
    ln -sfn "$BIN" "$USER_BIN/$name"
    chown -h "$USER_NAME:$USER_NAME" "$USER_BIN/$name"
done

cat <<EOF

==> ssh-authd installed for $USER_NAME.

    Normal ssh use for $USER_NAME now uses root's SSH key
    (/root/.ssh/id_ed25519) without a sudo prompt.

    SSH to GitHub from $USER_NAME is refused; use the llm-git-guard
    proxy (http://127.0.0.1:9419/<owner>/<repo>.git) for git work, or
    \`sudo ssh -i /root/.ssh/id_ed25519 git@github.com\` for explicit
    debugging.

    Sanity check:
        sudo -u $USER_NAME -i ssh -T git@github.com   # should refuse
        sudo -u $USER_NAME -i ssh -T <other-host>     # should use the key
EOF
