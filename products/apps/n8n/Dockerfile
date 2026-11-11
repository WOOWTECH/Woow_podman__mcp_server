# syntax=docker/dockerfile:1.7
# n8n MCP Admin + bundled 'n8n-mcp' (Node) — build context = monorepo ROOT
#   docker build -t woow-n8n-mcp-server -f apps/n8n/Dockerfile .

# ── Stage 1: frontend (Vite, workspace-aware) ───────────────────────────────
FROM node:26.12.1-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/mcp-admin-ui ./packages/mcp-admin-ui
COPY apps/n8n/frontend ./apps/n8n/frontend
RUN npm ci --no-audit --no-fund
RUN npm run build -w apps/n8n/frontend
# 產物：/app/apps/n8n/frontend/dist

# ── Node.js 執行環境來源：官方映像，以多架構 index digest 固定（vk、opendesign 用同一個 digest） ──
FROM node:26.12.1-bookworm-slim@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe AS node-runtime

# ── Stage 2: python + node runtime ──────────────────────────────────────────
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

# Node.js 26.12.1（2026-10-28 起 Active LTS；n8n-mcp 2.96.2 的 engines 為 ^22.23.4 || ^24.22.1 || >=26.12.1）：
# 只從上面固定 digest 的官方映像複製 node 與 npm，不用 nodesource apt 源。npm／npx 照官方 tarball 的相對連結重建，
# 建置時確認版本。
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
 && npm --version

# n8n-mcp（由 admin 以子行程啟動）。專用 lock 固定 n8n-mcp 2.96.2，並以 npm overrides 把其 MCP SDK
# 精確固定為 1.32.2；不與 root frontend workspace lock 混用。ARG 只是下面檢查用的期望值（也是 release
# train 清冊讀的釘點）：實際安裝內容由 lock 決定，兩者不一致時建置失敗。
# better-sqlite3 也以 overrides 固定：n8n-mcp 宣告的 optional ^11.10.0 沒有 Node 24／26（ABI 137／147）預建檔，
# npm ci 會靜默略過、上游退回 sql.js（記憶體約多一倍、沒有 FTS5，search_nodes 排序變差）。12.11.1 有 Node 26
# linux x64／arm64 預建檔（package.json 的 allowScripts 只核可這一版的 install script，換版要重新核可）；
# 選擇性相依裝失敗不會讓 npm ci／npm ls 失敗，所以這裡直接讀版本，下面另以 sqlite_check 確認真的載入原生模組。
ARG N8N_MCP_VERSION=2.96.2
ARG N8N_MCP_SDK_VERSION=1.32.2
ARG BETTER_SQLITE3_VERSION=12.11.1
COPY apps/n8n/upstream-dependencies/package.json apps/n8n/upstream-dependencies/package-lock.json /opt/n8n-mcp-upstream/
RUN npm ci --prefix /opt/n8n-mcp-upstream --omit=dev --no-audit --no-fund \
    && GLOBAL_NODE_MODULES="$(npm root -g)" \
    && ln -s /opt/n8n-mcp-upstream/node_modules/n8n-mcp "$GLOBAL_NODE_MODULES/n8n-mcp" \
    && npm ls --prefix /opt/n8n-mcp-upstream "n8n-mcp@${N8N_MCP_VERSION}" "@modelcontextprotocol/sdk@${N8N_MCP_SDK_VERSION}" --all \
    && test "$(node -p "require('/opt/n8n-mcp-upstream/node_modules/n8n-mcp/package.json').version")" = "${N8N_MCP_VERSION}" \
    && test "$(node -p "require('/opt/n8n-mcp-upstream/node_modules/@modelcontextprotocol/sdk/package.json').version")" = "${N8N_MCP_SDK_VERSION}" \
    && test "$(node -p "require('/opt/n8n-mcp-upstream/node_modules/better-sqlite3/package.json').version")" = "${BETTER_SQLITE3_VERSION}" \
    && npm cache clean --force

# 固定 npm artifact 的 direct inputs.type 相容性修補；未知版本／內容在建置時即失敗。
COPY apps/n8n/n8n_mcp_admin/upstream_direct_patch.py /tmp/upstream_direct_patch.py
RUN python /tmp/upstream_direct_patch.py "$(npm root -g)/n8n-mcp" \
    && node --check "$(npm root -g)/n8n-mcp/dist/mcp/handlers-n8n-manager.js" \
    && rm /tmp/upstream_direct_patch.py

# Keep n8n's backend reason alongside fixed test-run hints; input digest includes the direct patch above.
COPY apps/n8n/n8n_mcp_admin/upstream_message_patch.py /tmp/upstream_message_patch.py
RUN python /tmp/upstream_message_patch.py "$(npm root -g)/n8n-mcp" \
    && node --check "$(npm root -g)/n8n-mcp/dist/mcp/handlers-n8n-manager.js" \
    && rm /tmp/upstream_message_patch.py

# 工具回 {success:false}（n8n API 拒絕等）時 MCP 結果要 isError=true，n8n_delete_workflow 描述註明需先
# deactivate（商用驗收 DATA-NMCP-005-2）。同樣只認釘版的檔案位元組，未知版本／內容在建置時即失敗。
COPY apps/n8n/n8n_mcp_admin/upstream_result_patch.py /tmp/upstream_result_patch.py
RUN python /tmp/upstream_result_patch.py "$(npm root -g)/n8n-mcp" \
    && node --check "$(npm root -g)/n8n-mcp/dist/mcp/server.js" \
    && node --check "$(npm root -g)/n8n-mcp/dist/mcp/tools-n8n-manager.js" \
    && rm /tmp/upstream_result_patch.py

# credentials 的原生 action map 遺漏；只補已覆核分流，public proxy 仍即時執行 saved policy。
COPY apps/n8n/n8n_mcp_admin/upstream_policy_patch.py /tmp/upstream_policy_patch.py
RUN python /tmp/upstream_policy_patch.py "$(npm root -g)/n8n-mcp" \
    && node --check "$(npm root -g)/n8n-mcp/dist/mcp/tools-n8n-manager.js" \
    && rm /tmp/upstream_policy_patch.py

# n8n-mcp 2.96.2 validate_workflow returns object-valued warning/error details.
# Correct the two exact output-schema literals so MCP SDK clients accept them.
COPY apps/n8n/n8n_mcp_admin/upstream_output_schema_patch.py /tmp/upstream_output_schema_patch.py
RUN python /tmp/upstream_output_schema_patch.py "$(npm root -g)/n8n-mcp" \
    && node --check "$(npm root -g)/n8n-mcp/dist/mcp/tools.js" \
    && rm /tmp/upstream_output_schema_patch.py

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/n8n/pyproject.toml ./apps/n8n/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package n8n-mcp-admin

COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/n8n/ ./apps/n8n/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package n8n-mcp-admin

COPY --from=frontend-builder /app/apps/n8n/frontend/dist /app/static

# 建置時驗證上游 HTTP 入口與工具清單來源（上游改目錄結構時在 CI 就失敗，而不是開通後才起不來）
RUN PKG="$(npm root -g)/n8n-mcp" \
 && test -f "$PKG/dist/mcp/index.js" \
 && node apps/n8n/n8n_mcp_admin/upstream_catalog.js "$PKG" \
    | python -c "import json,sys; d=json.loads(sys.stdin.read().splitlines()[-1]); n=len(d['tools']); assert n >= 28, n; print('[n8n-mcp]', d['version'], n, 'tools')"

# 商用驗收 DATA-NMCP-003-3：管理頁 Session Timeout 說明寫「上游約每 5 分鐘清掃一次」；上游改清掃週期時在 CI 就失敗。
# 閘道回收閒置 session（mcp_admin_core.session_reclaim）認得上游「名額已滿」的 429 文字、靠 DELETE /mcp 立刻釋放名額；
# 上游改了這兩件事時也在 CI 就失敗。
RUN python -c "import sys; from n8n_mcp_admin.child_env import verify_upstream_session_sweep, verify_upstream_session_limit_reply; sys.exit(verify_upstream_session_sweep() or verify_upstream_session_limit_reply())"

# woow_paas_platform#1615：以 launcher 的環境實際載入上游 telemetry 設定，確認匿名使用統計是關的
# （模擬設定檔 enabled=true、繼承 env 寫 false 仍要關；上游改開關名稱時在 CI 就失敗）。同一步也確認
# npm 版本檢查（N8N_MCP_VERSION_CHECK_DISABLED）回 disabled 且沒有對 registry.npmjs.org 發請求。
RUN python -m n8n_mcp_admin.telemetry_check

# 以 launcher 的環境、上游自己的 createDatabaseAdapter 開一份種子 catalog 暫存複本：必須選到原生 better-sqlite3
# （行程內真的載入 better_sqlite3.node）、FTS5 可查、版本等於上面的釘版；靜默退回 sql.js 時在這裡建置失敗。
RUN python -m n8n_mcp_admin.sqlite_check --expect-version "${BETTER_SQLITE3_VERSION}"

# 非 root 執行（woow_paas_platform#1530）：uid／gid 1000 對齊 chart mcp-server 的 fsGroup 1000。
# 程式碼、venv、全域套件維持 root 擁有（app 使用者唯讀），只有 /data（config.json）與 HOME 可寫。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data
COPY deploy/shared/bootstrap.py /app/bootstrap.py
USER 1000:1000
VOLUME /data
EXPOSE 8080
CMD ["python", "/app/bootstrap.py", "n8n_mcp_admin.main:app"]
