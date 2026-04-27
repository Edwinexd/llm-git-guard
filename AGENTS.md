# AGENTS.md

Notes for AI agents (and humans) working on this repo.

## What this repo is

`llm-git-guard` is a **local git smart-HTTP proxy** that sits between
the host machine and GitHub. It runs as a Docker container bound to
`127.0.0.1:9419`, maintains bare mirrors of every cloned repo under
`/var/lib/llm-git-guard/repos`, and validates every push through a
`pre-receive` hook before forwarding to GitHub with root's SSH key.

This is **not** a Claude Code hook, a git client wrapper, or a commit
message linter for any one repo. It is server-side infrastructure —
the validation runs inside `git-receive-pack` on the proxy, after the
client's `git push` has already shipped the pack.

## Don't confuse it with

- **`~/.claude/hooks/no-claude-attribution.py`** — a Claude Code
  PreToolUse hook that lints `git commit` / `gh pr create` *commands*
  before they run. Lives in `~/.claude/`, fires inside the editor
  session, has nothing to do with this repo. If a request is "block
  commits with X" and the human is talking about Claude's own
  behaviour mid-session, that's the hook to edit. If it's "block
  pushes to GitHub with X", it's *this* repo.
- **`~/.local/bin/gh-sync`** — symlinks here but the user-facing
  install lives at `/opt/llm-git-guard/scripts/gh-sync`.

## Layout

```
hooks/pre-receive         bash, runs inside the bare mirror under git-receive-pack;
                          this is where validation rules live (vendor tokens,
                          force-push, deletion budget, subject hygiene, CLAUDE.md
                          symlink policy, etc.)
src/llm_git_guard/        FastAPI server: proxies smart-HTTP, refreshes mirrors,
                          installs the pre-receive hook into each mirror
config/exempt-repos.txt   per-repo exemptions (vendor-token only; force-push and
                          subject hygiene still apply)
scripts/install.sh        copies the repo to /opt/llm-git-guard, builds the image,
                          brings it up under docker compose
scripts/rewrite-origins.sh   repoint existing client clones at 127.0.0.1:9419
scripts/ssh-authd*        user-facing ssh wrapper; refuses GitHub targets and
                          forwards everything else via root's key
docker-compose.yml        the deployed unit
```

## Dev / deploy loop

The repo at `~/repos/Edwinexd/llm-git-guard` is the **source of
truth**. The deployed copy lives at `/opt/llm-git-guard/` and is what
the running container mounts. They drift — the install script copied
once, then the source has gained features the install never picked
up. To deploy a change:

```sh
sudo cp -a ~/repos/Edwinexd/llm-git-guard/. /opt/llm-git-guard/
cd /opt/llm-git-guard
sudo docker compose build
sudo docker compose up -d
```

For iteration without rebuilding, run the FastAPI server directly per
the README "Development" section.

## House rules enforced by `pre-receive`

These are the things to check and update when adding a new rule:

1. Read the env var at the top of the script next to the others
   (`LLMGG_*` naming, sane default).
2. Add the check inside the per-ref `while read -r old new ref` loop.
   The `commits` array already holds the new commits being introduced
   (handles both fast-forward and new-branch cases via the
   `--boundary` fork-point heuristic).
3. Decide if the rule is gated on `(( ! exempt ))` (vendor-identifier
   policy) or unconditional (style policy). Subject hygiene is
   unconditional; vendor regex is exempt-aware.
4. On rejection: `say "REJECT $ref: …"` and `fail=1`. Do **not**
   `exit 1` mid-loop — the existing flow accumulates failures so the
   user sees every problem in one push, not whack-a-mole.
5. Document the new rule in `README.md`'s "What it blocks" list and
   the config table.

Current rules:
- ref deletions blocked for the default branch and `LLMGG_PROTECTED_REFS_RE`
- non-fast-forward (force-push / rewind) blocked
- ref name matches `LLMGG_FORBIDDEN_RE`
- commit author/committer/message matches forbidden regex
- added line matches forbidden regex (with a per-file allow for
  `.gitignore` entries that look like agent-scratch-dir patterns)
- deletes more than `LLMGG_MAX_DELETED_LINES` lines or
  `LLMGG_MAX_DELETED_FILES` files
- if `CLAUDE.md` exists in the pushed tip, it must be a symlink
  (mode 120000) pointing to `AGENTS.md`
- commit subject longer than `LLMGG_SUBJECT_MAX` chars
- commit message has any non-empty body (single-line subjects only)
- commit message contains ` -- ` (space-dash-dash-space)

## Commit message rules for *this* repo

The pre-receive hook applies the subject-hygiene rules to its own
pushes too. So commits here must be:
- single line, no body
- ≤72 chars
- no ` -- ` anywhere in the message
- no Claude attribution footer (the Claude Code hook also blocks this
  client-side)

If you need an explanation longer than the subject can hold, put it in
a PR description.

## Why CLAUDE.md is a symlink

The pre-receive hook (lines ~184-201) rejects any push whose tip
contains a `CLAUDE.md` that isn't a symlink to `AGENTS.md`. Canonical
agent docs live in `AGENTS.md`; `CLAUDE.md` exists only so
Claude-flavoured tooling that searches for that filename finds the
same content. A regular `CLAUDE.md` file would mean a divergent,
likely agent-authored copy — exactly the thing this guard exists to
block. So when adding agent guidance, edit `AGENTS.md` and never
touch `CLAUDE.md` directly.
