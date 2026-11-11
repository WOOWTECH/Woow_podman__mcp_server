# syntax=docker/dockerfile:1.7
# Open Design MCP Admin —— 打包 vendored od_mcp_server(python) + 原生 HTTP launcher 的 MCP 管理殼。
# build context = monorepo ROOT（Dockerfile 依賴 root 的 npm / uv workspace）：
#   docker build -t woow-opendesign-mcp-server -f apps/opendesign/Dockerfile .
#
# 分層與關鍵決策（照 apps/vk 前例，但少一個 binary 下載 stage）：
#   - 共用核心 mcp-admin-core 提供 app factory / auth / config store / process manager / proxy。
#   - MCP server 是 vendored python（opendesign_mcp_server），隨 app 一併 COPY 進 image，
#     故不需 vk 那樣的 binary 下載＋sha256 驗證 stage；子行程為 venv console script
#     `opendesign-mcp-server`（原生 Streamable HTTP＋SSE 單一行程，#1369；seed 契約見 k8s-deploy.yaml）。
#   - MCP 子行程「不在」image 啟動時拉起：全交 McpProcessManager 依 config.json 的
#     mcp_server.command 於執行期 spawn，設定變更（Save & Restart）才會真正生效。CMD 只跑 uvicorn
#     admin（零設定啟動時 command="" → pm 跳過、admin 照聽 8080，CI /healthz smoke 因此成立）。

# ── Stage 1：frontend（Vite，workspace-aware，照 n8n / odoo-manage / vk 前例） ─────────
FROM node:26.12.1-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/mcp-admin-ui ./packages/mcp-admin-ui
COPY apps/opendesign/frontend ./apps/opendesign/frontend
RUN npm ci --no-audit --no-fund
RUN npm run build -w apps/opendesign/frontend
# 產物：/app/apps/opendesign/frontend/dist

# ── Node.js 執行環境來源：官方映像，以多架構 index digest 固定（與 n8n 同一個 digest） ──────────
FROM node:26.12.1-bookworm-slim@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe AS node-runtime

# ── Stage 2：python + node runtime（照 apps/vk/Dockerfile 混合 image 前例） ────────────
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

# ⚠️ 紅字：PATH 保留 /app/.venv/bin 在前 —— seed 的 command "opendesign-mcp-server" 是 venv
#    console script，McpProcessManager 以 execvp 走 PATH 解析；不在 PATH 前段子行程就起不來。

# Node.js 26.12.1 + supergateway：#1369 起原生 launcher 已不需要（舊 seed 會在 admin 啟動時自動遷移），
# 暫留以縮小本次變更面；確認各環境都已遷移後可移除整段以縮小 image。Node 從固定 digest 的官方映像複製
# （不用 nodesource apt 源）。
# Node 26 起官方 linux 執行檔動態連結 libatomic.so.1（24 以前沒有），python:3.12-slim 沒有這個函式庫：
# 只裝 Debian 的 libatomic1（不跨 stage 複製 .so）。缺了它 node 起不來，緊接的 node --version 檢查會讓建置失敗。
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && test "$(node --version)" = v26.12.1 \
 && npm install -g supergateway@3.4.3 \
 && npm cache clean --force

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/opendesign/pyproject.toml ./apps/opendesign/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package woow-opendesign-mcp-server

COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/opendesign/ ./apps/opendesign/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package woow-opendesign-mcp-server

COPY --from=frontend-builder /app/apps/opendesign/frontend/dist /app/static

# 非 root 執行（woow_paas_platform#1530）：uid／gid 1000 對齊 chart mcp-server 的 fsGroup 1000。
# 程式碼、venv、全域套件維持 root 擁有（app 使用者唯讀），只有 /data（config.json）與 HOME 可寫。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data
COPY deploy/shared/bootstrap.py /app/bootstrap.py
USER 1000:1000
VOLUME /data
EXPOSE 8080
# entrypoint 廢除：image 只跑 uvicorn admin；MCP 子行程（opendesign-mcp-server）由
# McpProcessManager 於執行期依 seed 的 mcp_server.command 拉起，此處不啟動任何 MCP 子行程。
CMD ["python", "/app/bootstrap.py", "opendesign_mcp_admin.main:app"]
