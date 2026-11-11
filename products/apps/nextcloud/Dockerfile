# syntax=docker/dockerfile:1.7
# Nextcloud MCP Admin —— vendored nextcloud_mcp_server（FastMCP，9 工具，app password）的 MCP 管理殼。
# build context = monorepo ROOT（Dockerfile 依賴 root 的 npm / uv workspace）：
#   docker build -t woow-nextcloud-mcp-server -f apps/nextcloud/Dockerfile .
# registry／k8s 映像名：nextcloud-mcp-admin（CI retag）。
#
# 分層與關鍵決策（照 apps/litellm；MCP server 是純 python，不需 node runtime）：
#   - 共用核心 mcp-admin-core 提供 app factory / auth / config store / process manager / proxy。
#   - MCP 子行程「不在」image 啟動時拉起：McpProcessManager 依 config.json 的 mcp_server.command
#     （nextcloud-mcp-server launcher，只綁 127.0.0.1）於執行期 spawn。CMD 只跑 uvicorn admin
#     （零設定啟動時 command="" → 不 spawn、admin 照聽 8080，CI /healthz smoke 因此成立）。
#   - SPA 一律放 /app/static：core create_app 預設找 $CWD/static（WORKDIR /app）。
#   - Python 依賴全由 root uv.lock 釘版（--frozen），前端由 root package-lock.json 釘版（npm ci）；
#     不在執行期 git clone／pip install。

# ── Stage 1：frontend（Vite，workspace-aware） ──────────────────────────────────────
FROM node:26.12.1-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/mcp-admin-ui ./packages/mcp-admin-ui
COPY apps/nextcloud/frontend ./apps/nextcloud/frontend
RUN npm ci --no-audit --no-fund
RUN npm run build -w apps/nextcloud/frontend
# 產物：/app/apps/nextcloud/frontend/dist

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

# ⚠️ PATH 保留 /app/.venv/bin 在前：seed 的 command "nextcloud-mcp-server" 是 venv console script，
#    McpProcessManager 以 execvp 走 PATH 解析；不在 PATH 前段子行程就起不來。
# ⚠️ MCP_ADMIN_CONFIG 也是子行程 launcher 讀 Tools 頁政策（tools.disabled）的位置；讀不到就唯讀啟動。

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/nextcloud/pyproject.toml ./apps/nextcloud/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package woow-nextcloud-mcp-server

COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/nextcloud/ ./apps/nextcloud/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package woow-nextcloud-mcp-server

COPY --from=frontend-builder /app/apps/nextcloud/frontend/dist /app/static

# 建置期自檢：SPA 在 core 會找的位置、launcher 在 PATH、admin／子行程模組 import 得起來、
# Tools 頁 registry 9 支＝vendored 工具面，且子行程全開時實際註冊 9 支（不連 Nextcloud）。
RUN test -f /app/static/index.html \
    && command -v nextcloud-mcp-server \
    && python -c "import asyncio, nextcloud_mcp_admin.main, nextcloud_mcp_admin.launcher, nextcloud_mcp_admin.tool_registry as r; from nextcloud_mcp_server.tools import ALL_TOOLS; from nextcloud_mcp_server.settings import Settings; from nextcloud_mcp_server.server import create_server; assert len(r.TOOL_REGISTRY) == 9 and r.get_all_tool_names() == list(ALL_TOOLS); s = Settings(base_url='https://nextcloud.invalid', username='selfcheck', app_password='selfcheck-pw', readonly=False, allow_delete=True); names = [t.name for t in asyncio.run(create_server(s).list_tools())]; assert sorted(names) == sorted(ALL_TOOLS), names; print('nextcloud tools:', len(names))"

# 非 root 執行（woow_paas_platform#1530）：uid／gid 1000 對齊 chart mcp-server 的 fsGroup 1000。
# 程式碼、venv、全域套件維持 root 擁有（app 使用者唯讀），只有 /data（config.json）與 HOME 可寫。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data
COPY deploy/shared/bootstrap.py /app/bootstrap.py
USER 1000:1000
VOLUME /data
EXPOSE 8080
CMD ["python", "/app/bootstrap.py", "nextcloud_mcp_admin.main:app"]
