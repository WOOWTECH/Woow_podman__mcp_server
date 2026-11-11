# syntax=docker/dockerfile:1.7
# Hermes MCP Admin + Server (pure-python) — build context = monorepo ROOT
#   docker build -t woow-hermes-mcp-server -f apps/hermes/Dockerfile .

# ── Stage 1: frontend (Vite, workspace-aware) ───────────────────────────────
FROM node:26.12.1-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/mcp-admin-ui ./packages/mcp-admin-ui
COPY apps/hermes/frontend ./apps/hermes/frontend
RUN npm ci --no-audit --no-fund
RUN npm run build -w apps/hermes/frontend
# 產物：/app/apps/hermes/frontend/dist

# ── Stage 2: python runtime ─────────────────────────────────────────────────
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.6.4 /uv /uvx /bin/

# 相依由 uv 管理；移除基底 pip／ensurepip 的舊 vendored certifi（GHSA-6q48-6c7w-9gw2）。
RUN python -m pip uninstall --yes pip \
 && rm -rf /usr/local/lib/python3.12/ensurepip

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH" \
    MCP_ADMIN_CONFIG=/data/config.json

WORKDIR /app

# 1) 相依層（可快取）：lock + 此 app closure 內的 manifest
COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/hermes/pyproject.toml ./apps/hermes/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package woow-hermes-mcp-server

# 2) 專案層：只 COPY 核心 + 本 app（不含其他 app）
COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/hermes/ ./apps/hermes/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package woow-hermes-mcp-server

COPY --from=frontend-builder /app/apps/hermes/frontend/dist /app/static

# 非 root 執行（woow_paas_platform#1530）：uid／gid 1000 對齊 chart mcp-server 的 fsGroup 1000。
# 程式碼、venv、全域套件維持 root 擁有（app 使用者唯讀），只有 /data（config.json）與 HOME 可寫。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data
COPY deploy/shared/bootstrap.py /app/bootstrap.py
USER 1000:1000
VOLUME /data
EXPOSE 8080
CMD ["python", "/app/bootstrap.py", "hermes_mcp_admin.main:app"]
