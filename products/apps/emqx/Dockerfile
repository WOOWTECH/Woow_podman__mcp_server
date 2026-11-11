# syntax=docker/dockerfile:1.7
# EMQX MCP Admin —— vendored emqx_mcp_server（FastMCP，39 tools, API key/secret）的 MCP 管理殼。
# build context = monorepo ROOT（Dockerfile 依賴 root 的 npm / uv workspace）：
#   docker build -t woow-emqx-mcp-server -f apps/emqx/Dockerfile .
# registry／k8s 映像名：emqx-mcp-admin（CI retag）。
#
# 分層與關鍵決策（照 apps/litellm；MCP server 是純 python，不需 node runtime）：
#   - 共用核心 mcp-admin-core 提供 app factory / auth / config store / process manager / proxy。
#   - CMD 啟動 uvicorn admin；core lifespan 以固定 LaunchPolicy 啟動
#     emqx-mcp-server launcher（只綁 127.0.0.1），不執行設定檔中的任意 command。
#     （admin 可於設定後端前啟動；後端未設定不代表就緒）。
#   - SPA 一律放 /app/static：core create_app 預設找 $CWD/static（WORKDIR /app）。
#   - Python 依賴全由 root uv.lock 釘版（--frozen），前端由 root package-lock.json 釘版（npm ci）；
#     不在執行期 git clone／pip install。

# ── Stage 1：frontend（Vite，workspace-aware） ──────────────────────────────────────
FROM node:26.12.1-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/mcp-admin-ui ./packages/mcp-admin-ui
COPY apps/emqx/frontend ./apps/emqx/frontend
RUN npm ci --no-audit --no-fund
RUN npm run build -w apps/emqx/frontend
# 產物：/app/apps/emqx/frontend/dist

# ── Stage 2：python runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.6.4 /uv /uvx /bin/

# 相依由 uv 管理；移除基底 pip／ensurepip 的舊 vendored certifi（GHSA-6q48-6c7w-9gw2）。
RUN python -m pip uninstall --yes pip \
 && rm -rf /usr/local/lib/python3.12/ensurepip

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH" \
    MCP_ADMIN_CONFIG=/data/config.json \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_CHECK_FOR_UPDATES=off

# ⚠️ PATH 保留 /app/.venv/bin 在前：LaunchPolicy 的 "emqx-mcp-server" 是 venv console script，
#    McpProcessManager 以 execvp 走 PATH 解析；不在 PATH 前段子行程就起不來。
# ⚠️ MCP_ADMIN_CONFIG 也是子行程 launcher 讀 Tools 頁政策（tools.disabled）的位置；讀不到時拒絕所有工具呼叫。

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/emqx/pyproject.toml ./apps/emqx/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package woow-emqx-mcp-server

COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/emqx/ ./apps/emqx/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package woow-emqx-mcp-server

COPY --from=frontend-builder /app/apps/emqx/frontend/dist /app/static

# Root uv/npm locks include this app without changing existing dependency records.
# Actual child protocol/HTTP-policy smoke is an explicit CI step, not image acceptance.
RUN test -f /app/static/index.html \
    && command -v emqx-mcp-server \
    && python -c "import emqx_mcp_admin.main; from emqx_mcp_admin.tool_registry import TOOL_REGISTRY; assert len(TOOL_REGISTRY) == 39"

# 非 root 執行（woow_paas_platform#1530）：uid／gid 1000 對齊 chart mcp-server 的 fsGroup 1000。
# 程式碼、venv、全域套件維持 root 擁有（app 使用者唯讀），只有 /data（config.json）與 HOME 可寫。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data
COPY deploy/shared/bootstrap.py /app/bootstrap.py
USER 1000:1000
VOLUME /data
EXPOSE 8080
CMD ["python", "/app/bootstrap.py", "emqx_mcp_admin.main:app"]
