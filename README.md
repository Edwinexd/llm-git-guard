# llm-git-guard

A local git smart-HTTP proxy that sits between your machine and GitHub and
refuses pushes that look like the sort of accident an over-eager LLM agent
might cause: force pushes, ref deletions, massive deletions, or commits that
carry obvious vendor fingerprints.

## How it works

```
  ~/repos/owner/repo/.git      ------>   http://127.0.0.1:9419/owner/repo.git
  (remote.origin.url)                          |
                                               |   llm-git-guard (container)
                                               |    - FastAPI + Uvicorn
                                               |    - maintains a bare mirror
                                               |      at /var/lib/llm-git-guard
                                               |      /repos/owner/repo.git
                                               |    - on fetch: refreshes
                                               |      from GitHub with root's
                                               |      key
                                               |    - on push: validates in a
                                               |      pre-receive hook, then
                                               |      forwards to GitHub
                                               v
                                         git@github.com:owner/repo.git
```

- The SSH key talking to GitHub lives at `/root/.ssh/id_ed25519` on the host
  and is mounted read-only into the container. The unprivileged user account
  cannot read it, so the only path from that user to GitHub is the proxy.
- The only change on each client repo is `remote.origin.url`; everything
  else (auto-cloning mirrors, refreshing, validating, forwarding) happens
  inside the container.

## What it blocks

The `pre-receive` hook reads `<old> <new> <ref>` lines from `git-receive-pack`
and rejects the push if any ref:

- is being deleted *and* is protected — the repo's default branch (taken from
  the mirror's `HEAD` symref) or any ref matching `LLMGG_PROTECTED_REFS_RE`
  (default: `main`, `master`, `develop`, `trunk`, `prod`, `production`,
  `release/*`). Deletion of ordinary topic / feature branches is allowed and
  forwarded to GitHub.
- isn't a fast-forward (force push or rewind),
- has a name that matches the forbidden-vendor regex,
- contains a commit whose author, committer, subject, or body matches the regex,
- adds any line that matches the regex,
- exceeds the configured line or file deletion cap.

If every ref passes, the hook forwards them atomically to the `upstream`
remote with `git push --atomic`. Only if upstream accepts does the local
mirror ref update, so the mirror and GitHub never drift.

Per-repo exceptions live in `/etc/llm-git-guard/exempt-repos.txt` (one
`owner/name` per line). Exempt repos still have force-push and deletion
limits enforced — only vendor-token checks are skipped.

## Install

Requirements: Docker Engine with the `compose` plugin.

```sh
git clone https://github.com/Edwinexd/llm-git-guard ~/llm-git-guard
sudo ~/llm-git-guard/scripts/install.sh
```

That:

1. copies the repo to `/opt/llm-git-guard`,
2. creates `/var/lib/llm-git-guard/repos` (data) and `/etc/llm-git-guard/`
   (config),
3. builds the image and brings the container up on `127.0.0.1:9419` with
   `restart: unless-stopped` so it survives reboots.

Then, from your user account:

```sh
# Repoint existing clones.
/opt/llm-git-guard/scripts/rewrite-origins.sh

# Make future clones use the proxy (replaces the old gh-sync).
ln -sf /opt/llm-git-guard/scripts/gh-sync ~/.local/bin/gh-sync
```

Finally, move the SSH key out of your user account so there's no way for a
process running as you to bypass the proxy:

```sh
sudo install -m 600 -o root -g root ~/.ssh/id_ed25519 /root/.ssh/id_ed25519
sudo install -m 600 -o root -g root ~/.ssh/known_hosts /root/.ssh/known_hosts
shred -u ~/.ssh/id_ed25519       # keep .pub if you want to see what's registered
```

## Uninstall

```sh
sudo /opt/llm-git-guard/scripts/uninstall.sh           # stops the service
sudo /opt/llm-git-guard/scripts/uninstall.sh --purge   # also removes data + config
```

## Configuration

All settings are environment variables read by the container. Override them
in `docker-compose.yml` or with a `.env` file next to it.

| var | default | meaning |
| --- | --- | --- |
| `LLMGG_BIND` | `0.0.0.0` | interface to bind inside the container |
| `LLMGG_PORT` | `9419` | port to listen on |
| `LLMGG_REPOS_DIR` | `/var/lib/llm-git-guard/repos` | where bare mirrors live |
| `LLMGG_SSH_KEY` | `/root/.ssh/id_ed25519` | key used to reach GitHub |
| `LLMGG_KNOWN_HOSTS` | `/root/.ssh/known_hosts` | known_hosts for GitHub |
| `LLMGG_UPSTREAM_TEMPLATE` | `git@github.com:{owner}/{repo}.git` | how upstream URLs are built |
| `LLMGG_REFRESH_INTERVAL` | `30` (s) | minimum gap between upstream refreshes per mirror |
| `LLMGG_MAX_DELETED_LINES` | `2000` | reject any ref whose update deletes more lines |
| `LLMGG_MAX_DELETED_FILES` | `50` | ... or more files |
| `LLMGG_FORBIDDEN_RE` | vendor pattern | regex applied case-insensitively |
| `LLMGG_PROTECTED_REFS_RE` | `^refs/heads/(main\|master\|develop\|trunk\|prod\|production\|release/.*)$` | refs whose deletion is always refused (default branch is also protected dynamically) |
| `LLMGG_LOG_LEVEL` | `info` | Python/Uvicorn log level |

## Development

Run directly (outside the container) for iteration:

```sh
pip install -r requirements.txt
LLMGG_REPOS_DIR=./repos \
LLMGG_HOOKS_DIR=$(pwd)/hooks \
LLMGG_CONFIG_DIR=$(pwd)/config \
LLMGG_SSH_KEY=$HOME/.ssh/id_ed25519 \
LLMGG_KNOWN_HOSTS=$HOME/.ssh/known_hosts \
python -m llm_git_guard
```

Or rebuild the container in place:

```sh
cd /opt/llm-git-guard
sudo docker compose build
sudo docker compose up -d
```

Logs:

```sh
sudo docker compose -f /opt/llm-git-guard/docker-compose.yml logs -f
```

## Threat model

This is a guardrail, not a sandbox. It catches a cooperative but careless
agent doing something destructive. A determined attacker with `sudo NOPASSWD:
ALL` can obviously defeat any local policy (e.g., by reading the key out of
the container). If you care about the latter, restrict sudo separately.

## License

MIT. See `LICENSE`.
