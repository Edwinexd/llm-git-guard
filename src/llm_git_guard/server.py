#!/usr/bin/env python3
"""llm-git-guard: local git smart-HTTP proxy with safety validation.

Clients point ``remote.origin.url`` at ``http://127.0.0.1:9419/<owner>/<repo>.git``.
The daemon keeps a bare mirror per repo under REPOS_DIR, with a remote named
``upstream`` that points at ``git@github.com:<owner>/<repo>.git``. On fetch we
refresh the mirror (using root's SSH key) and serve it through
``git-http-backend``. On push we let ``git-http-backend`` run a ``pre-receive``
hook which validates the incoming refs and then forwards accepted refs to
upstream. Only if upstream accepts does the local ref update land, so the
mirror stays in sync.
"""

from __future__ import annotations

import http.server
import logging
import os
import re
import socketserver
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

REPOS_DIR = Path(os.environ.get("LLMGG_REPOS_DIR", "/var/lib/llm-git-guard/repos"))
HOOKS_DIR = Path(os.environ.get("LLMGG_HOOKS_DIR", "/opt/llm-git-guard/hooks"))
CONFIG_DIR = Path(os.environ.get("LLMGG_CONFIG_DIR", "/etc/llm-git-guard"))
SSH_KEY = os.environ.get("LLMGG_SSH_KEY", "/root/.ssh/id_ed25519")
KNOWN_HOSTS = os.environ.get("LLMGG_KNOWN_HOSTS", "/root/.ssh/known_hosts")
BIND_ADDR = os.environ.get("LLMGG_BIND", "127.0.0.1")
BIND_PORT = int(os.environ.get("LLMGG_PORT", "9419"))
REFRESH_INTERVAL = int(os.environ.get("LLMGG_REFRESH_INTERVAL", "30"))
GIT_HTTP_BACKEND = os.environ.get(
    "LLMGG_GIT_HTTP_BACKEND", "/usr/lib/git-core/git-http-backend"
)
UPSTREAM_TEMPLATE = os.environ.get(
    "LLMGG_UPSTREAM_TEMPLATE", "git@github.com:{owner}/{repo}.git"
)
CLONE_TIMEOUT = int(os.environ.get("LLMGG_CLONE_TIMEOUT", "300"))
FETCH_TIMEOUT = int(os.environ.get("LLMGG_FETCH_TIMEOUT", "120"))

log = logging.getLogger("llm-git-guard")

# Accepts /<owner>/<repo>[.git]/<rest>. Owner and repo are restricted to the
# characters GitHub allows so we can't be tricked into escaping REPOS_DIR.
PATH_RE = re.compile(
    r"^/(?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"/(?P<repo>[A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?/(?P<rest>.+)$"
)


def ssh_command() -> str:
    return (
        f"/usr/bin/ssh -i {SSH_KEY} -o IdentitiesOnly=yes "
        f"-o UserKnownHostsFile={KNOWN_HOSTS} "
        f"-o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    )


def ssh_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = ssh_command()
    return env


# ---- mirror management ------------------------------------------------------

_provision_locks: dict[str, threading.Lock] = {}
_provision_meta_lock = threading.Lock()


def _lock_for(repo_id: str) -> threading.Lock:
    with _provision_meta_lock:
        lock = _provision_locks.get(repo_id)
        if lock is None:
            lock = _provision_locks[repo_id] = threading.Lock()
        return lock


def ensure_mirror(owner: str, repo: str) -> Path:
    """Return the path to a bare mirror for owner/repo, cloning it if needed."""
    repo_id = f"{owner}/{repo}"
    repo_dir = REPOS_DIR / owner / f"{repo}.git"
    if repo_dir.exists():
        _ensure_hooks(repo_dir)
        return repo_dir
    with _lock_for(repo_id):
        if repo_dir.exists():
            _ensure_hooks(repo_dir)
            return repo_dir
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        upstream = UPSTREAM_TEMPLATE.format(owner=owner, repo=repo)
        log.info("provisioning mirror %s -> %s", repo_id, repo_dir)
        subprocess.run(
            ["/usr/bin/git", "clone", "--mirror", upstream, str(repo_dir)],
            env=ssh_env(),
            check=True,
            timeout=CLONE_TIMEOUT,
        )
        # Rename the default remote to "upstream" so the hook can
        # unambiguously forward pushes there.
        subprocess.run(
            [
                "/usr/bin/git", "-C", str(repo_dir),
                "remote", "rename", "origin", "upstream",
            ],
            check=True,
        )
        _ensure_hooks(repo_dir)
    return repo_dir


def _ensure_hooks(repo_dir: Path) -> None:
    hooks = repo_dir / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-receive",):
        src = HOOKS_DIR / name
        dst = hooks / name
        # Keep hooks as symlinks so upgrading llm-git-guard updates every
        # mirror in one shot.
        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            dst.symlink_to(src)
        except OSError as e:
            log.warning("could not install hook %s: %s", dst, e)


_refresh_last: dict[Path, float] = {}
_refresh_lock = threading.Lock()


def refresh_mirror(repo_dir: Path) -> None:
    """Fetch upstream into ``repo_dir``, rate-limited to REFRESH_INTERVAL."""
    now = time.time()
    with _refresh_lock:
        last = _refresh_last.get(repo_dir, 0.0)
        if now - last < REFRESH_INTERVAL:
            return
        _refresh_last[repo_dir] = now
    try:
        subprocess.run(
            [
                "/usr/bin/git", "-C", str(repo_dir),
                "fetch", "--prune", "--quiet", "upstream",
            ],
            env=ssh_env(),
            check=True,
            timeout=FETCH_TIMEOUT,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        log.warning("refresh failed for %s: %s", repo_dir, e)


# ---- HTTP handler -----------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # These are the only endpoints clients should hit.
    _ALLOWED_REST = {
        "info/refs",
        "git-upload-pack",
        "git-receive-pack",
        "HEAD",
    }

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        split = urlsplit(self.path)
        m = PATH_RE.match(split.path)
        if not m:
            self._send_text(404, "not a git smart-http path\n")
            return
        owner, repo, rest = m["owner"], m["repo"], m["rest"]
        if rest not in self._ALLOWED_REST:
            self._send_text(404, f"unsupported endpoint: {rest}\n")
            return

        try:
            repo_dir = ensure_mirror(owner, repo)
        except subprocess.CalledProcessError as e:
            log.error("clone failed %s/%s: %s", owner, repo, e)
            self._send_text(502, f"upstream clone failed: {e}\n")
            return
        except subprocess.TimeoutExpired:
            self._send_text(504, "upstream clone timed out\n")
            return

        query = split.query
        if "service=git-upload-pack" in query or rest == "git-upload-pack":
            service = "git-upload-pack"
        elif "service=git-receive-pack" in query or rest == "git-receive-pack":
            service = "git-receive-pack"
        else:
            service = ""

        # Refresh before fetches so readers see the current upstream state.
        if service == "git-upload-pack":
            refresh_mirror(repo_dir)

        env = {
            "GIT_PROJECT_ROOT": str(REPOS_DIR),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": f"/{owner}/{repo}.git/{rest}",
            "REQUEST_METHOD": self.command,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": self.headers.get("Content-Length", "") or "",
            "REMOTE_ADDR": self.client_address[0],
            "REMOTE_USER": "llm-git-guard",
            # These propagate into the pre-receive hook so it knows which
            # GitHub repo this push is destined for.
            "LLMGG_OWNER": owner,
            "LLMGG_REPO": repo,
            "LLMGG_SSH_KEY": SSH_KEY,
            "LLMGG_KNOWN_HOSTS": KNOWN_HOSTS,
            "LLMGG_CONFIG_DIR": str(CONFIG_DIR),
            "LLMGG_HOOKS_DIR": str(HOOKS_DIR),
            "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
        }
        ce = self.headers.get("Content-Encoding")
        if ce:
            env["HTTP_CONTENT_ENCODING"] = ce
        gp = self.headers.get("Git-Protocol")
        if gp:
            env["HTTP_GIT_PROTOCOL"] = gp

        body_len = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(body_len) if body_len > 0 else b""

        try:
            proc = subprocess.Popen(
                [GIT_HTTP_BACKEND],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            self._send_text(500, f"git-http-backend not found at {GIT_HTTP_BACKEND}\n")
            return

        out, err = proc.communicate(body)
        if err:
            for line in err.decode(errors="replace").splitlines():
                log.info("backend[%s %s/%s]: %s", service or "?", owner, repo, line)

        # Split CGI response into headers + body (handle both CRLF and LF).
        header_blob, sep, data = out.partition(b"\r\n\r\n")
        if not sep:
            header_blob, sep, data = out.partition(b"\n\n")
        if not sep:
            # No header separator -- something went wrong; surface as 500.
            self._send_text(500, "malformed backend response\n")
            return

        status = 200
        resp_headers: list[tuple[str, str]] = []
        for raw in header_blob.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                text = line.decode("latin-1")
            except Exception:
                continue
            if text.lower().startswith("status:"):
                try:
                    status = int(text.split(":", 1)[1].strip().split()[0])
                except Exception:
                    status = 500
                continue
            if ":" in text:
                k, _, v = text.partition(":")
                resp_headers.append((k.strip(), v.strip()))

        self.send_response(status)
        has_len = False
        for k, v in resp_headers:
            if k.lower() == "content-length":
                has_len = True
            self.send_header(k, v)
        if not has_len:
            self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _send_text(self, code: int, msg: str) -> None:
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        log.info("%s - %s", self.address_string(), fmt % args)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LLMGG_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingServer((BIND_ADDR, BIND_PORT), Handler)
    log.info(
        "llm-git-guard listening on %s:%d (repos=%s, hooks=%s)",
        BIND_ADDR, BIND_PORT, REPOS_DIR, HOOKS_DIR,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
