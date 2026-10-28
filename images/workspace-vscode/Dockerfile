# Editor tier for Python / Jupyter / Streamlit users. Tiny by design: heavy work goes to warehouses.
# Build from the repo root: docker build -f images/workspace-vscode/Dockerfile -t fdp/workspace-vscode:dev .

# --- Floren Data Platform runner extension (sidebar + live run consoles) ---
FROM node:20-bookworm-slim AS extension
WORKDIR /build/extensions/vscode-fdp
COPY schemas /build/schemas
COPY extensions/vscode-fdp/package.json extensions/vscode-fdp/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY extensions/vscode-fdp ./
RUN npm run package

# --- code-server ---
FROM codercom/code-server:4.93.1

# Editor extensions first: they change rarely, so SDK changes below don't re-download them.
# From Open VSX (code-server's default), open-source only, pinned to versions that support
# code-server 4.93's VS Code engine (1.93); bump together with code-server. One extension per
# step with retries: installing several in one step intermittently fails to unpack (ENOENT).
USER coder
RUN for i in 1 2 3; do code-server --install-extension ms-python.python@2024.14.1 && exit 0; sleep 2; done; exit 1
RUN for i in 1 2 3; do code-server --install-extension ms-toolsai.jupyter@2024.8.1 && exit 0; sleep 2; done; exit 1
RUN for i in 1 2 3; do code-server --install-extension charliermarsh.ruff@2026.88.0 && exit 0; sleep 2; done; exit 1
# Type checker: optional (its ~10k-file stub folder is the most fragile to unpack).
RUN for i in 1 2 3; do code-server --install-extension detachhead.basedpyright@1.29.0 && exit 0; sleep 2; done; \
    echo "WARNING: basedpyright not installed (type checking unavailable in this image)"

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
 && rm -rf /var/lib/apt/lists/*

# Debian's system Python is "externally managed"; analysts get their own venv on PATH.
RUN python3 -m venv /opt/venv && chown -R 1000:1000 /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY sdk/python /tmp/sdk
RUN pip install --no-cache-dir /tmp/sdk[all] ipykernel jupyter pandas streamlit ruff \
 && rm -rf /tmp/sdk

RUN mkdir -p /workspace && chown 1000:1000 /workspace
USER coder
COPY --from=extension /build/extensions/vscode-fdp/fdp.vsix /tmp/fdp.vsix
RUN code-server --install-extension /tmp/fdp.vsix
COPY --chown=1000:1000 images/workspace-vscode/settings.json /home/coder/.local/share/code-server/User/settings.json

# Git: settings and SSH keys persist on the workspace volume (see fdp-git-setup.sh); HTTPS tokens come from the
# platform through git-credential-fdp, never from a file.
COPY images/common/fdp-git-setup.sh images/common/git-credential-fdp images/workspace-vscode/fdp-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/fdp-entrypoint.sh", "--bind-addr", "0.0.0.0:8080", "."]

WORKDIR /workspace
