#!/usr/bin/env bash
# Rewrite every ~/repos/<owner>/<repo>/.git origin URL to the local
# llm-git-guard proxy. Idempotent; safe to re-run.
set -euo pipefail

ROOT="${REPOS_DIR:-$HOME/repos}"
PROXY="${LLMGG_PROXY:-http://127.0.0.1:9419}"

count=0
updated=0
skipped=0

shopt -s nullglob
for owner_dir in "$ROOT"/*/; do
    owner=$(basename "$owner_dir")
    for repo_dir in "$owner_dir"*/; do
        [[ -d "$repo_dir/.git" ]] || continue
        repo=$(basename "$repo_dir")
        count=$((count+1))
        url=$(git -C "$repo_dir" config --get remote.origin.url 2>/dev/null || true)
        if [[ -z "$url" ]]; then
            skipped=$((skipped+1))
            continue
        fi
        new="$PROXY/$owner/$repo.git"
        if [[ "$url" == "$new" ]]; then
            skipped=$((skipped+1))
            continue
        fi
        echo "$owner/$repo: $url -> $new"
        git -C "$repo_dir" remote set-url origin "$new"
        updated=$((updated+1))
    done
done

echo "done: $updated repointed, $skipped unchanged, $count total"
