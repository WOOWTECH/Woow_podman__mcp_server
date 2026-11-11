# syntax=docker/dockerfile:1.7
# Build with: podman build -f products/deploy/headscale/Dockerfile -t local/headscale-mcp-admin:VERSION products/
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/
RUN python -m pip uninstall --yes pip && rm -rf /usr/local/lib/python3.12/ensurepip

ARG VCS_REF
LABEL org.opencontainers.image.title="Headscale MCP Podman" \
      org.opencontainers.image.source="https://github.com/WOOWTECH/Woow_podman__mcp_server" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH" \
    MCP_ADMIN_CONFIG=/data/config.json \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_CHECK_FOR_UPDATES=off

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/headscale/pyproject.toml ./apps/headscale/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package woow-headscale-mcp-server

COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/headscale/ ./apps/headscale/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package woow-headscale-mcp-server

COPY deploy/headscale/bootstrap.py /app/bootstrap.py
RUN test -f /app/apps/headscale/static/index.html \
 && command -v headscale-mcp-server \
 && groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data \
 && chmod -R a+rX /app/apps /app/packages

USER 1000:1000
RUN python -c "import headscale_mcp_admin.main, headscale_mcp_admin.launcher; from pathlib import Path; assert Path('/app/apps/headscale/static/index.html').read_text()"
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"
CMD ["python", "/app/bootstrap.py"]
