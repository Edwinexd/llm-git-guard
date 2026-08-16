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
- exceeds the configured line or file deletion cap,
- contains a commit whose subject is longer than `LLMGG_SUBJECT_MAX` chars,
  whose message has any non-empty body, or whose message contains
  ` -- ` (space-dash-dash-space). These subject-hygiene rules apply even
  to repos in `exempt-repos.txt`.

If every ref passes, the hook forwards them atomically to the `upstream`
remote with `git push --atomic`. Only if upstream accepts does the local
mirror ref update, so the mirror and GitHub never drift.

Per-repo exceptions live in `/etc/llm-git-guard/exempt-repos.txt` (one
`owner/name` per line). Exempt repos still have force-push and deletion
limits enforced — only vendor-token checks are skipped.

## Client-side hooks

The rules above run inside `git-receive-pack`, which means you find out a
commit was wrong only once the whole pack has shipped and the push is
rejected. On a large push that is a bad trade: a single over-long subject
fifty commits back rejects the lot, and fixing it is a history rewrite.

`scripts/install-hooks` puts the same rules in front of your own git:

```sh
install-hooks                       # the repo you are standing in
install-hooks ~/repos/Edthing/ae-dev
install-hooks --all                 # every git repo under ~/repos
install-hooks --uninstall
```

It writes two hooks:

| hook | when | what it checks |
|---|---|---|
| `commit-msg` | `git commit` | subject cap, empty body, no `" -- "`, no vendor name in the message |
| `pre-push` | `git push` | every rule in `hooks/pre-receive`, over the refs about to be sent |

Neither hook contains a copy of the rules. Both invoke the same
`hooks/pre-receive` the proxy runs, through its `LLMGG_CHECK_MESSAGE` and
`LLMGG_VALIDATE_ONLY` entry points, because a second copy of the rules is a
copy that drifts and a client check that disagrees with the server is worse
than no client check.

For the same reason the hooks refuse to use a `pre-receive` that predates
those entry points: an old one would read their stdin as a push, find no
refs and exit 0, which reads exactly like a pass. They say so on stderr and
let the command through rather than blocking your work silently. If you see
that message, deploy `/opt/llm-git-guard` (see Install).

Two things the client cannot know for certain: whether the server will treat
the repo as exempt, and, for a branch the remote has never seen, exactly
which commits it will treat as new. So the two can disagree in either
direction — a clean local run can still meet a server rejection. Treat the
hooks as a way to catch the ordinary mistake early, not as proof the push
will land.

An existing `commit-msg` or `pre-push` that this script did not write is left
alone unless you pass `--force`.

## Bypass for one push

When a rule is in the way and you've decided the push is safe (recovering
from a destructive accident, importing a vendored chunk that trips the
regex, etc.), use the wrapper instead of hand-rolling a direct-to-GitHub
remote:

```sh
git bypass-push                       # current branch
git bypass-push --force-with-lease    # rewind a feature branch
git bypass-push origin :refs/heads/x  # delete a protected branch
```

The wrapper refuses unless both stdin and stdout are TTYs and you type
`YES` at the prompt. It then attaches the contents of
`/etc/llm-git-guard/bypass-token` as an `X-LLMGG-Bypass` header on a normal
`git push` against the proxy URL — so `--force-with-lease` and any other
client-side checks behave exactly as they would on an ordinary push. The
proxy validates the header (constant-time compare), the pre-receive hook
skips every rule, and the resulting push is forwarded to GitHub with
force-prefixed refspecs.

The TTY gate is the actual safety property: the token file is readable by
your user, so an agent running as you could in principle construct the
header, but it would need an interactive terminal to satisfy the wrapper.
If you want stronger isolation, tighten the token file's group ownership.

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

# Catch violations at commit and push time instead of at the proxy.
ln -sf /opt/llm-git-guard/scripts/install-hooks ~/.local/bin/install-hooks
install-hooks --all
```

Finally, move the SSH key out of your user account so there's no way for a
process running as you to bypass the proxy:

```sh
sudo install -m 600 -o root -g root ~/.ssh/id_ed25519 /root/.ssh/id_ed25519
sudo install -m 600 -o root -g root ~/.ssh/known_hosts /root/.ssh/known_hosts
shred -u ~/.ssh/id_ed25519       # keep .pub if you want to see what's registered
```

### Optional: ssh-authd wrapper

Once the SSH key lives only at `/root/.ssh/id_ed25519`, the user account has
no working `ssh` for any host that was authorised with that key. Two ways to
get it back:

- Run `sudo ssh -i /root/.ssh/id_ed25519 user@host` when you need it. No
  extra setup, but noisy.
- Install `ssh-authd`:

  ```sh
  sudo /opt/llm-git-guard/scripts/install-ssh-authd.sh <username>
  ```

  `ssh-authd` is a tiny wrapper that a) refuses GitHub SSH targets, and
  b) forwards everything else to a privileged helper that runs
  `/usr/bin/ssh` with root's key under a narrow passwordless sudo rule.
  The installer symlinks `~<username>/.local/bin/{ssh,scp,sftp}` to the
  wrapper, so normal tools pick it up via PATH.

  Net effect for that user:
  * `ssh other-server`, `scp`, `sftp`, and anything else that calls `ssh`
    via `PATH` — work without any sudo prompt, using root's key.
  * `ssh git@github.com` / `scp file github.com:/tmp/` — refused with a
    hint pointing at the llm-git-guard proxy.
  * `sudo ssh -i /root/.ssh/id_ed25519 git@github.com` — still works for
    deliberate debugging; the wrapper is only in the user's PATH.

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
| `LLMGG_SUBJECT_MAX` | `72` | max commit subject length (chars). New commits with longer subjects, non-empty bodies, or ` -- ` in the message are rejected. Applies to all repos including those in `exempt-repos.txt`. |
| `LLMGG_FORBIDDEN_RE` | vendor pattern | regex applied case-insensitively |
| `LLMGG_PROTECTED_REFS_RE` | `^refs/heads/(main\|master\|develop\|trunk\|prod\|production\|release/.*)$` | refs whose deletion is always refused (default branch is also protected dynamically) |
| `LLMGG_BYPASS_TOKEN_FILE` | `/etc/llm-git-guard/bypass-token` | path to the shared secret accepted in `X-LLMGG-Bypass`. Read once at startup; restart the container to rotate. Empty / missing disables the bypass feature entirely. |
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
