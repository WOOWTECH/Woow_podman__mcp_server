# syntax=docker/dockerfile:1.7
# VK MCP Admin —— 打包 supergateway(npm) + vibe-kanban-mcp(Rust binary) 的 MCP 管理殼。
# build context = monorepo ROOT（Dockerfile 依賴 root 的 npm / uv workspace）：
#   docker build -t woow-vk-mcp-server -f apps/vk/Dockerfile .
#
# 分層與關鍵決策：
#   - 共用核心 mcp-admin-core 提供 app factory / auth / config store / process manager / proxy。
#   - MCP 子行程鏈「不在」image 啟動時拉起：來源 entrypoint.sh 雙行程做法已廢除；
#     supergateway 全交 McpProcessManager 依 config.json 的 mcp_server.command 於執行期 spawn，
#     設定變更（Save & Restart）才會真正生效。CMD 只跑 uvicorn admin（零設定啟動時
#     command="" → pm 跳過、admin 照聽 8080，CI /healthz smoke 因此成立）。
#   - vibe-kanban-mcp binary 不進 git（供應鏈斷點）：改由 Stage A 以 ARG 驅動的 URL 下載，
#     並用 sha256sum 驗證後才落地；日後切內網 mirror 只改 ARG 預設值、不動結構。

# ── Stage A：取 vibe-kanban-mcp binary（ARG 驅動；預設 = 上游 R2 CDN） ────────────────
# ⚠️ CI 的 build step 為固定樣板、不帶 --build-arg（.gitea/workflows/ci.yml），
#    故此處預設 VK_MCP_URL 必須是 CI runner 實際可達的 URL；日後改走內網 Artifactory
#    generic mirror 時，只需覆寫這三個 ARG 預設值（URL + 對應 sha256）。
FROM debian:bookworm-slim AS vk-binary
ARG VK_MCP_TAG=v0.1.44-20260424091429
ARG VK_MCP_URL=https://npm-cdn.vibekanban.com/binaries/${VK_MCP_TAG}/linux-x64/vibe-kanban-mcp.zip
ARG VK_MCP_SHA256=ce1df1be160a4d8aa085fa5836db7a698f1109a379e3f8018bd937bee8546299
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates unzip \
 && curl -fsSL "$VK_MCP_URL" -o /tmp/vk.zip \
 && echo "${VK_MCP_SHA256}  /tmp/vk.zip" | sha256sum -c - \
 && unzip /tmp/vk.zip -d /out \
 && chmod +x /out/vibe-kanban-mcp

# ── Stage B：frontend（Vite，workspace-aware，照 n8n / odoo-manage 前例） ─────────────
FROM node:26.12.1-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/mcp-admin-ui ./packages/mcp-admin-ui
COPY apps/vk/frontend ./apps/vk/frontend
RUN npm ci --no-audit --no-fund
RUN npm run build -w apps/vk/frontend
# 產物：/app/apps/vk/frontend/dist

# ── Node.js 執行環境來源：官方映像，以多架構 index digest 固定（與 n8n 同一個 digest） ──────────
FROM node:26.12.1-bookworm-slim@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe AS node-runtime

# ── Stage C：python + node runtime（照 apps/n8n/Dockerfile 混合 image 前例） ──────────
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

# Node.js 26.12.1（2026-10-28 起 Active LTS；從固定 digest 的官方映像複製 node 與 npm，不用 nodesource apt 源）
# + supergateway（pin 版本：未 pin 有 --outputTransport / --healthEndpoint 語意漂移風險；3.4.3 是 3.x 最新版）。
# supergateway 由 admin 以子行程啟動（seed mcp_server.command="supergateway"），此處僅預裝全域 bin。
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

# 從 Stage A 帶入已 sha256 驗證的 vibe-kanban-mcp binary（supergateway --stdio 會 execvp 它）。
COPY --from=vk-binary /out/vibe-kanban-mcp /usr/local/bin/vibe-kanban-mcp

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/vk/pyproject.toml ./apps/vk/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package woow-vk-mcp-server

COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/vk/ ./apps/vk/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package woow-vk-mcp-server

COPY --from=frontend-builder /app/apps/vk/frontend/dist /app/static

# 非 root 執行（woow_paas_platform#1530）：uid／gid 1000 對齊 chart mcp-server 的 fsGroup 1000。
# 程式碼、venv、全域套件維持 root 擁有（app 使用者唯讀），只有 /data（config.json）與 HOME 可寫。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data
USER 1000:1000
VOLUME /data
EXPOSE 8080
# entrypoint 廢除：image 只跑 uvicorn admin；supergateway + vibe-kanban-mcp 由 McpProcessManager
# 於執行期依 seed 的 mcp_server.command 拉起，此處不啟動任何 MCP 子行程。
CMD ["uvicorn", "vk_mcp_admin.main:app", "--host", "0.0.0.0", "--port", "8080"]
