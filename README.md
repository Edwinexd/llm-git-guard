# llm-git-guard

A small local git smart-HTTP proxy that sits between your machine and GitHub
and refuses pushes that look like the kind of thing an over-eager LLM agent
might do by accident: force pushes, ref deletions, gigantic deletions, or
commits that carry obvious vendor fingerprints.

## How it works

```
  ~/repos/owner/repo/.git      ------>   http://127.0.0.1:9419/owner/repo.git
  (remote.origin.url)                          |
                                               |  llm-git-guard (systemd)
                                               |   - maintains a bare mirror
                                               |     at /var/lib/llm-git-guard/
                                               |     repos/owner/repo.git
                                               |   - on fetch: refreshes from
                                               |     GitHub with root's key
                                               |   - on push: validates in a
                                               |     pre-receive hook, then
                                               |     pushes to GitHub
                                               v
                                         git@github.com:owner/repo.git
```

The SSH key used to talk to GitHub lives at `/root/.ssh/id_ed25519` and is
only readable by root. The user account has no path to push to GitHub
directly; their only remote is the local HTTP proxy.

The only change needed on each client repo is `remote.origin.url`; everything
else (cloning mirrors on demand, refreshing them, validating, forwarding) is
handled by the daemon.

## What it blocks

For each pushed ref, the pre-receive hook rejects the push if any of:

- the ref is being deleted (`new == 0000…`),
- it's not a fast-forward (force push / rewind),
- the ref name matches the forbidden-vendor regex,
- any commit's author, committer, subject, or body matches the regex,
- any added line in the diff matches the regex,
- the total deletions exceed the configured line or file cap.

If every ref passes, the hook forwards them atomically to `upstream` with
`git push --atomic`. Only if that succeeds does the local mirror accept the
update, so the mirror never drifts from GitHub.

Per-repo exceptions live in `/etc/llm-git-guard/exempt-repos.txt` (one
`owner/name` per line). Exempt repos still have force-push and deletion caps
enforced; only the vendor-token checks are skipped.

## Install

```sh
git clone https://github.com/Edwinexd/llm-git-guard ~/llm-git-guard
sudo ~/llm-git-guard/scripts/install.sh
```

That:

1. copies sources to `/opt/llm-git-guard`,
2. creates `/var/lib/llm-git-guard/repos` and `/etc/llm-git-guard/`,
3. installs the `llm-git-guard.service` unit and starts it on
   `127.0.0.1:9419`.

Then, from your user account:

```sh
# Repoint existing clones.
/opt/llm-git-guard/scripts/rewrite-origins.sh

# Make future clones use the proxy (replaces the old gh-sync).
ln -sf /opt/llm-git-guard/scripts/gh-sync ~/.local/bin/gh-sync
```

Finally, remove the SSH key from your user account so there is no way for a
rogue process running as you to bypass the proxy:

```sh
sudo install -m 600 -o root -g root ~/.ssh/id_ed25519 /root/.ssh/id_ed25519
sudo install -m 600 -o root -g root ~/.ssh/known_hosts /root/.ssh/known_hosts
rm ~/.ssh/id_ed25519  # keep .pub if you want to see what's registered
```

## Configuration

All settings are environment variables on the service (see
`systemd/llm-git-guard.service`):

| var | default | meaning |
| --- | --- | --- |
| `LLMGG_BIND` | `127.0.0.1` | interface to bind |
| `LLMGG_PORT` | `9419` | port to listen on |
| `LLMGG_REPOS_DIR` | `/var/lib/llm-git-guard/repos` | where bare mirrors live |
| `LLMGG_SSH_KEY` | `/root/.ssh/id_ed25519` | key used to reach GitHub |
| `LLMGG_KNOWN_HOSTS` | `/root/.ssh/known_hosts` | known_hosts for GitHub |
| `LLMGG_UPSTREAM_TEMPLATE` | `git@github.com:{owner}/{repo}.git` | where to clone/push from |
| `LLMGG_REFRESH_INTERVAL` | `30` (s) | minimum gap between upstream refreshes per mirror |
| `LLMGG_MAX_DELETED_LINES` | `2000` | reject any ref whose update deletes more lines |
| `LLMGG_MAX_DELETED_FILES` | `50` | ... or more files |
| `LLMGG_FORBIDDEN_RE` | vendor pattern | regex applied case-insensitively |

## Threat model

This is a guardrail, not a sandbox. It is designed to catch a cooperative but
careless automation agent doing something destructive. A determined attacker
running as your user with `sudo NOPASSWD: ALL` can obviously defeat any local
policy. If you care about the latter, restrict sudo separately.

## License

MIT. See `LICENSE`.
