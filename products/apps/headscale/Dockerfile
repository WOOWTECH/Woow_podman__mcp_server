# syntax=docker/dockerfile:1.7
# 從 monorepo 根目錄建置；Headscale 專用靜態介面不需要 Node/npm。
# docker build -f apps/headscale/Dockerfile -t local/headscale:candidate .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.6.4 /uv /uvx /bin/

ARG VCS_REF
LABEL org.opencontainers.image.title="Headscale MCP" \
      org.opencontainers.image.source="https://git-prod.woowtech.io/WOOWTECH/woow-mcp-server" \
      org.opencontainers.image.revision="${VCS_REF}"

# 與家族映像一致：不保留基底 pip/ensurepip 內舊版 vendored certifi。
RUN python -m pip uninstall --yes pip \
 && rm -rf /usr/local/lib/python3.12/ensurepip

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

# Root 維護的原生 HTML/CSS/JS；不複製其他 app 的管理介面。
COPY apps/headscale/static/ ./static/
RUN test -f /app/static/index.html \
 && command -v headscale-mcp-server \
 && python -c "from importlib.util import find_spec; assert find_spec('headscale_mcp_admin.main'); assert find_spec('headscale_mcp_admin.launcher'); assert find_spec('headscale_mcp_server.server')"

# chart seed-init 使用同一映像的 python3；設定檔只在 PVC 不存在時建立。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data \
 && chmod -R a+rX /app/apps /app/packages /app/static
USER 1000:1000
# Verify the actual runtime identity can import code and read the dedicated UI.
RUN python -c "import headscale_mcp_admin.main, headscale_mcp_admin.launcher; from pathlib import Path; assert Path('/app/apps/headscale/static/index.html').read_text()"
VOLUME /data
EXPOSE 8080
CMD ["uvicorn", "headscale_mcp_admin.main:app", "--host", "0.0.0.0", "--port", "8080"]
