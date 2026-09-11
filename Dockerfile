# syntax=docker/dockerfile:1
#
# Woow Podman MCP Admin: production image
# =======================================
# Two-stage build, mirroring the LiteLLM reference:
#
#   stage 1 (node 22)        builds the React/Vite SPA straight from frontend/src with
#                            `npm ci` against the committed frontend/package-lock.json;
#                            no overrides, no build-time string rewriting.
#   stage 2 (python 3.12)    installs the mcp-admin-core wheel first, then the product
#                            package with the [admin] extra, copies the built SPA into the
#                            static dir, and serves the admin console (which spawns the
#                            FastMCP child) on :8080.
#
# Both base images are pinned to exact versions (STANDARD section 3). Bump them in a
# separate change and re-run the build job in .github/workflows/tests.yml.
#
# Deploy with scripts/install.sh, not by hand: it builds this image as
# localhost/woow-podman-mcp-admin:$(cat VERSION) and installs the Quadlet units, which
# mount the rootless podman socket directory, publish on 127.0.0.1 only and pass
# JWT_SECRET / ADMIN_PASSWORD / MCP_AUTH_TOKEN as podman secrets.
#
# NOTE: no secrets are baked into the image. The Podman socket path and API version are
# supplied at runtime via the Admin GUI / ConfigStore (/data/config.json) or the PODMAN_*
# environment variables.
#
# SECURITY: mounting the Podman socket grants this container full control of the host's
# containers; it is root-equivalent for that user. Mount the *rootless* socket, never the
# root one, and keep the default `safe` profile unless you specifically need destructive
# tools.

# ---------------------------------------------------------------------------
# Stage 1 — build the SPA
# ---------------------------------------------------------------------------
FROM docker.io/library/node:22.23.2-alpine3.24 AS frontend
WORKDIR /build

# Install deps first for better layer caching. `npm ci` installs exactly what the
# committed lockfile records and fails if package.json and the lockfile disagree.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# Bring in the shared SPA sources.
COPY frontend/ ./

# NOTE: the reference used to COPY a frontend-overrides/ConnectionConfig.jsx over
# the page source and then `sed` residual branding out of the shared SPA. Both
# steps are deliberately absent here. The override silently reverted every fix
# made to frontend/src/pages/ConnectionConfig.jsx. frontend/src is the single
# source of truth; do not reintroduce either.

RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — python runtime
# ---------------------------------------------------------------------------
FROM docker.io/library/python:3.12.14-slim-trixie AS runtime

# Non-interactive, no .pyc, unbuffered logs (so SSE log streaming is live).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_ADMIN_CONFIG=/data/config.json

WORKDIR /app

# Install the shared admin-core package FIRST (its own pyproject), so the
# product install resolves against the already-present core.
COPY mcp_admin_core.pyproject.toml ./pyproject.toml
COPY mcp_admin_core/ ./mcp_admin_core/
RUN pip install .

# Now install the product packages (FastMCP server + admin layer) with the
# [admin] extra (fastapi/uvicorn/pyjwt/httpx).
COPY pyproject.toml ./pyproject.toml
COPY README.md ./README.md
COPY woow_podman_mcp_server/ ./woow_podman_mcp_server/
COPY podman_mcp_admin/ ./podman_mcp_admin/
RUN pip install ".[admin]"

# Copy the built SPA into the static dir the admin app serves from.
COPY --from=frontend /build/dist/ ./podman_mcp_admin/static/

# Config lives on a volume; seed dir exists so first-run write succeeds.
RUN mkdir -p /data && chmod 700 /data
VOLUME ["/data"]

# The admin console listens on 8080. The FastMCP child is spawned by the
# McpProcessManager and bound to loopback only — never exposed here.
EXPOSE 8080

# Basic liveness: the admin app exposes an unauthenticated /healthz.
#
# REQUIRES `podman build --format docker`. Podman defaults to the OCI image
# format, which has no healthcheck field, so this instruction is silently
# discarded on a default build — no error, no warning in the usual output, and
# the resulting container reports no health state at all. Verify after building:
#   podman inspect -f '{{.Config.Healthcheck.Test}}' <image>
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"

CMD ["uvicorn", "podman_mcp_admin.main:app", "--host", "0.0.0.0", "--port", "8080"]
