#!/usr/bin/env bash
# Tear down llm-git-guard: stop the container, remove the image, and leave
# data/config in place unless --purge is passed.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "uninstall.sh: must run as root" >&2
    exit 1
fi

PREFIX=/opt/llm-git-guard
DATA_DIR=/var/lib/llm-git-guard
CONFIG_DIR=/etc/llm-git-guard
PURGE=0

for a in "$@"; do
    case "$a" in
        --purge) PURGE=1 ;;
        *) echo "unknown arg: $a" >&2; exit 1 ;;
    esac
done

if [[ -d "$PREFIX" ]]; then
    (cd "$PREFIX" && docker compose down || true)
    docker image rm llm-git-guard:local >/dev/null 2>&1 || true
fi

if (( PURGE )); then
    rm -rf "$PREFIX" "$DATA_DIR" "$CONFIG_DIR"
    echo "purged $PREFIX, $DATA_DIR, $CONFIG_DIR"
else
    rm -rf "$PREFIX"
    echo "stopped service, kept $DATA_DIR and $CONFIG_DIR"
fi
