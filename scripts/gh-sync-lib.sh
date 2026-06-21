#!/usr/bin/env bash
# Shared helpers for the gh-sync family. Sourced, not executed.
#
#   gh-sync        full daily sync (clone missing + fetch existing) from cron
#   gh-sync-watch  live watcher that clones brand-new repos within ~a minute
#
# Keeping the clone/fetch/repoint/register logic and the proxy-URL convention in
# one place means both entrypoints stay in lockstep. Git traffic goes through
# llm-git-guard rather than direct SSH; the GitHub API listing still uses gh.

REPOS_DIR="${REPOS_DIR:-$HOME/repos}"
LOG_DIR="${LOG_DIR:-$HOME/.local/share/gh-sync}"
CLAUDE_PROJECTS_DIR="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
PROXY="${LLMGG_PROXY:-http://127.0.0.1:9419}"

mkdir -p "$REPOS_DIR" "$LOG_DIR" "$CLAUDE_PROJECTS_DIR"

# Mirror gh-sync's project registration: create the Happy/Claude project cache
# dir for a repo path (sanitized the same way Happy sanitizes paths).
register_project() {
    local path="$1" id
    id=$(printf '%s' "$path" | sed 's|[^a-zA-Z0-9-]|-|g')
    mkdir -p "$CLAUDE_PROJECTS_DIR/$id"
}

# Emit "owner<TAB>name" for every accessible, non-archived repo. Returns gh's
# exit status so callers can tell a real failure from an empty list.
list_remote_repos() {
    gh api --paginate \
        '/user/repos?affiliation=owner,collaborator,organization_member&per_page=100' \
        --jq '.[] | select(.archived == false) | "\(.owner.login)\t\(.name)"'
}

# Heal a clone that was created while its upstream was still empty. git clone
# of an empty repo writes no remote.origin.fetch refspec and parks HEAD at the
# refs/heads/.invalid sentinel; because we only ever fetch (never pull/checkout)
# the clone never recovers once upstream gains commits, leaving a working tree
# with an unresolvable HEAD that breaks tools running `git branch` there.
# Guarded on the broken state so a healthy checkout is never disturbed.
repair_clone() {
    local dest="$1"
    if ! git -C "$dest" config --get remote.origin.fetch >/dev/null 2>&1; then
        git -C "$dest" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
        git -C "$dest" fetch --prune --quiet 2>/dev/null || true
    fi
    if ! git -C "$dest" rev-parse --verify -q HEAD >/dev/null 2>&1; then
        git -C "$dest" remote set-head origin -a >/dev/null 2>&1 || true
        local def
        def=$(git -C "$dest" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null)
        def="${def#origin/}"
        if [[ -n "$def" ]] && git -C "$dest" rev-parse --verify -q "refs/remotes/origin/$def" >/dev/null 2>&1; then
            git -C "$dest" checkout -q "$def" 2>/dev/null \
                && echo "repair: $dest (healed empty-clone HEAD -> $def)"
        fi
    fi
}

# Clone if missing, else repoint origin to the proxy and fetch. Always registers.
# Echoes a status line; returns non-zero on clone/fetch failure.
sync_repo() {
    local owner="$1" name="$2"
    local dest="$REPOS_DIR/$owner/$name"
    local proxy_url="$PROXY/$owner/$name.git"
    local rc=0
    if [[ -d "$dest/.git" ]]; then
        local current
        current=$(git -C "$dest" config --get remote.origin.url 2>/dev/null || true)
        if [[ "$current" != "$proxy_url" ]]; then
            git -C "$dest" remote set-url origin "$proxy_url"
            echo "repoint: $owner/$name ($current -> $proxy_url)"
        fi
        if git -C "$dest" fetch --prune --quiet 2>/dev/null; then
            echo "fetch ok: $owner/$name"
        else
            echo "fetch FAIL: $owner/$name"; rc=1
        fi
        repair_clone "$dest"
    else
        mkdir -p "$REPOS_DIR/$owner"
        if git clone --quiet "$proxy_url" "$dest" 2>/dev/null; then
            echo "clone ok: $owner/$name"
        else
            echo "clone FAIL: $owner/$name"; rc=1
        fi
    fi
    register_project "$dest"
    return $rc
}

# Clone only if missing; never touches an existing checkout. Used by the live
# watcher so a freshly-detected repo lands without re-fetching the whole estate.
# Echoes a status line; returns non-zero if the clone failed.
clone_repo() {
    local owner="$1" name="$2"
    local dest="$REPOS_DIR/$owner/$name"
    local proxy_url="$PROXY/$owner/$name.git"
    if [[ -d "$dest/.git" ]]; then
        register_project "$dest"
        return 0
    fi
    mkdir -p "$REPOS_DIR/$owner"
    if git clone --quiet "$proxy_url" "$dest" 2>/dev/null; then
        echo "clone ok: $owner/$name"
        register_project "$dest"
        return 0
    fi
    echo "clone FAIL: $owner/$name"
    return 1
}
