"""llm-git-guard: FastAPI app that proxies git smart-HTTP to GitHub.

Clients point ``remote.origin.url`` at ``http://127.0.0.1:9419/<owner>/<repo>.git``.
For each (owner, repo) we keep a bare mirror under ``REPOS_DIR`` with a remote
``upstream`` pointing at GitHub. Fetches refresh the mirror from upstream and
then stream through ``git-http-backend``. Pushes stream into
``git-http-backend``, whose ``pre-receive`` hook validates the incoming refs
and forwards accepted refs to upstream using the SSH key inside this
container. Only when upstream accepts does the local ref update land, which
keeps the mirror in sync with GitHub.

The network path is intentionally boring: parse the URL, configure a CGI
environment, spawn ``git-http-backend``, stream bytes in both directions.
FastAPI + Uvicorn give us an async loop and real streaming, which matters
once multiple clients are cloning/pushing through the same instance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

# ---- configuration ---------------------------------------------------------

REPOS_DIR = Path(os.environ.get("LLMGG_REPOS_DIR", "/var/lib/llm-git-guard/repos"))
HOOKS_DIR = Path(os.environ.get("LLMGG_HOOKS_DIR", "/opt/llm-git-guard/hooks"))
CONFIG_DIR = Path(os.environ.get("LLMGG_CONFIG_DIR", "/etc/llm-git-guard"))
SSH_KEY = os.environ.get("LLMGG_SSH_KEY", "/root/.ssh/id_ed25519")
KNOWN_HOSTS = os.environ.get("LLMGG_KNOWN_HOSTS", "/root/.ssh/known_hosts")
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

# ``<owner>/<repo>[.git]/<rest>`` -- owner/repo charset matches what GitHub
# allows so a crafted URL cannot escape ``REPOS_DIR``.
PATH_RE = re.compile(
    r"^/(?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"/(?P<repo>[A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?/(?P<rest>.+)$"
)

# Only these rest-of-path values correspond to real smart-HTTP endpoints.
_ALLOWED_REST = {"info/refs", "git-upload-pack", "git-receive-pack", "HEAD"}


# ---- helpers --------------------------------------------------------------

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


# ---- mirror management ----------------------------------------------------

_provision_meta_lock = threading.Lock()
_provision_locks: dict[str, threading.Lock] = {}


def _lock_for(repo_id: str) -> threading.Lock:
    with _provision_meta_lock:
        lock = _provision_locks.get(repo_id)
        if lock is None:
            lock = _provision_locks[repo_id] = threading.Lock()
        return lock


def _ensure_hooks(repo_dir: Path) -> None:
    hooks = repo_dir / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-receive",):
        src = HOOKS_DIR / name
        dst = hooks / name
        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            dst.symlink_to(src)
        except OSError as e:
            log.warning("could not install hook %s: %s", dst, e)


def _ensure_mirror_sync(owner: str, repo: str) -> Path:
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
            env=ssh_env(), check=True, timeout=CLONE_TIMEOUT,
        )
        # Name the remote "upstream" so the hook can refer to it
        # unambiguously, and drop the mirror=true flag that ``clone --mirror``
        # leaves behind -- otherwise explicit refspecs on push are refused.
        # The fetch refspec ``+refs/*:refs/*`` is kept, which is what we want
        # so refreshes replicate every upstream ref into the mirror.
        subprocess.run(
            ["/usr/bin/git", "-C", str(repo_dir),
             "remote", "rename", "origin", "upstream"],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(repo_dir),
             "config", "--unset", "remote.upstream.mirror"],
            check=False,
        )
        _ensure_hooks(repo_dir)
    return repo_dir


async def ensure_mirror(owner: str, repo: str) -> Path:
    return await asyncio.to_thread(_ensure_mirror_sync, owner, repo)


_refresh_last: dict[Path, float] = {}
_refresh_lock = threading.Lock()


def _refresh_mirror_sync(repo_dir: Path) -> None:
    now = time.time()
    with _refresh_lock:
        last = _refresh_last.get(repo_dir, 0.0)
        if now - last < REFRESH_INTERVAL:
            return
        _refresh_last[repo_dir] = now
    try:
        subprocess.run(
            ["/usr/bin/git", "-C", str(repo_dir),
             "fetch", "--prune", "--quiet", "upstream"],
            env=ssh_env(), check=True, timeout=FETCH_TIMEOUT,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        log.warning("refresh failed for %s: %s", repo_dir, e)


async def refresh_mirror(repo_dir: Path) -> None:
    await asyncio.to_thread(_refresh_mirror_sync, repo_dir)


# ---- FastAPI app ----------------------------------------------------------

app = FastAPI(
    title="llm-git-guard",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route("/{_path:path}", methods=["GET", "POST"])
async def git_proxy(_path: str, request: Request):
    split_path = request.url.path  # already includes the leading slash
    m = PATH_RE.match(split_path)
    if not m:
        return PlainTextResponse("not a git smart-http path\n", status_code=404)
    owner, repo, rest = m["owner"], m["repo"], m["rest"]
    if rest not in _ALLOWED_REST:
        return PlainTextResponse(f"unsupported endpoint: {rest}\n", status_code=404)

    try:
        repo_dir = await ensure_mirror(owner, repo)
    except subprocess.CalledProcessError as e:
        log.error("clone failed %s/%s: %s", owner, repo, e)
        return PlainTextResponse(f"upstream clone failed: {e}\n", status_code=502)
    except subprocess.TimeoutExpired:
        return PlainTextResponse("upstream clone timed out\n", status_code=504)

    query = request.url.query or ""
    if "service=git-upload-pack" in query or rest == "git-upload-pack":
        service = "git-upload-pack"
    elif "service=git-receive-pack" in query or rest == "git-receive-pack":
        service = "git-receive-pack"
    else:
        service = ""

    # Refresh before fetches so callers see current upstream state.
    if service == "git-upload-pack":
        await refresh_mirror(repo_dir)

    env = {
        "GIT_PROJECT_ROOT": str(REPOS_DIR),
        "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": f"/{owner}/{repo}.git/{rest}",
        "REQUEST_METHOD": request.method,
        "QUERY_STRING": query,
        "CONTENT_TYPE": request.headers.get("content-type", ""),
        "CONTENT_LENGTH": request.headers.get("content-length", "") or "",
        "REMOTE_ADDR": request.client.host if request.client else "",
        "REMOTE_USER": "llm-git-guard",
        # Propagated into the pre-receive hook.
        "LLMGG_OWNER": owner,
        "LLMGG_REPO": repo,
        "LLMGG_SSH_KEY": SSH_KEY,
        "LLMGG_KNOWN_HOSTS": KNOWN_HOSTS,
        "LLMGG_CONFIG_DIR": str(CONFIG_DIR),
        "LLMGG_HOOKS_DIR": str(HOOKS_DIR),
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "HOME": os.environ.get("HOME", "/root"),
    }
    ce = request.headers.get("content-encoding")
    if ce:
        env["HTTP_CONTENT_ENCODING"] = ce
    gp = request.headers.get("git-protocol")
    if gp:
        env["HTTP_GIT_PROTOCOL"] = gp

    try:
        proc = await asyncio.create_subprocess_exec(
            GIT_HTTP_BACKEND,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return PlainTextResponse(
            f"git-http-backend not found at {GIT_HTTP_BACKEND}\n", status_code=500
        )

    async def feed_stdin() -> None:
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except Exception:
                pass

    feeder = asyncio.create_task(feed_stdin())

    # Parse CGI headers. git-http-backend uses LF; tolerate CRLF too.
    assert proc.stdout is not None
    status = 200
    headers: dict[str, str] = {}
    while True:
        raw = await proc.stdout.readline()
        if not raw or raw in (b"\r\n", b"\n"):
            break
        line = raw.rstrip(b"\r\n")
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
            headers[k.strip()] = v.strip()

    async def body_stream() -> AsyncIterator[bytes]:
        assert proc.stdout is not None
        try:
            while True:
                chunk = await proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                await feeder
            except Exception:
                pass
            rc = await proc.wait()
            if proc.stderr is not None:
                err = await proc.stderr.read()
                if err:
                    for line in err.decode(errors="replace").splitlines():
                        log.info(
                            "backend[%s %s/%s]: %s",
                            service or "?", owner, repo, line,
                        )
            if rc != 0:
                log.warning(
                    "backend exited rc=%d for %s %s/%s", rc, service, owner, repo
                )

    return StreamingResponse(body_stream(), status_code=status, headers=headers)


# ---- uvicorn entrypoint ---------------------------------------------------

def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LLMGG_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    uvicorn.run(
        "llm_git_guard.server:app",
        host=os.environ.get("LLMGG_BIND", "0.0.0.0"),
        port=int(os.environ.get("LLMGG_PORT", "9419")),
        log_level=os.environ.get("LLMGG_LOG_LEVEL", "info").lower(),
        access_log=True,
        proxy_headers=False,
        forwarded_allow_ips=None,
    )


if __name__ == "__main__":
    main()
