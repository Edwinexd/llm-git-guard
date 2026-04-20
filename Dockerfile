FROM python:3.13-slim

# git + openssh client for the outbound upstream push; ca-certificates so
# TLS works; tini as a proper PID 1 to reap git-http-backend zombies.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git openssh-client ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Python deps first so image layers cache well.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# App code and hooks live in well-known paths so the systemd unit on the host
# (if any) and the pre-receive hook agree with server.py's defaults.
COPY src/llm_git_guard /app/llm_git_guard
COPY hooks /opt/llm-git-guard/hooks

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    LLMGG_REPOS_DIR=/var/lib/llm-git-guard/repos \
    LLMGG_HOOKS_DIR=/opt/llm-git-guard/hooks \
    LLMGG_CONFIG_DIR=/etc/llm-git-guard \
    LLMGG_SSH_KEY=/root/.ssh/id_ed25519 \
    LLMGG_KNOWN_HOSTS=/root/.ssh/known_hosts \
    LLMGG_BIND=0.0.0.0 \
    LLMGG_PORT=9419

EXPOSE 9419

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9419/healthz',timeout=2).status==200 else 1)" \
    || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "llm_git_guard"]
