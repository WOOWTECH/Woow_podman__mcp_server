# syntax=docker/dockerfile:1.7
# Odoo MCP Admin + 'odoo-mcp-server' + mcp[cli] — build context = monorepo ROOT
#   docker build -t woow-odoo-mcp-server -f apps/odoo/Dockerfile .
# odoo-mcp-server / mcp[cli] 為 woow-odoo-mcp-server 的宣告 deps，uv sync 會裝進 venv。

# ── Stage 1: frontend (Vite, workspace-aware) ───────────────────────────────
FROM node:26.12.1-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/mcp-admin-ui ./packages/mcp-admin-ui
COPY apps/odoo/frontend ./apps/odoo/frontend
RUN npm ci --no-audit --no-fund
RUN npm run build -w apps/odoo/frontend
# 產物：/app/apps/odoo/frontend/dist

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

COPY pyproject.toml uv.lock ./
COPY packages/mcp-admin-core/pyproject.toml ./packages/mcp-admin-core/
COPY apps/odoo/pyproject.toml ./apps/odoo/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --package woow-odoo-mcp-server

COPY packages/mcp-admin-core/ ./packages/mcp-admin-core/
COPY apps/odoo/ ./apps/odoo/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package woow-odoo-mcp-server

# ── Elmo build-time patches（apps/odoo/patches/，已隨上一個 COPY 進 image）────────────
# 把「跨 session token store 修復 / Allow Direct Writes 開關」以 source patch 方式烙進已安裝的
# odoo_mcp 套件（版本在 apps/odoo/pyproject.toml 釘死 1.3.0）。兩個 patch 皆 idempotent、僅注入
# env-gated 程式碼；實際行為由子行程 runtime 讀 ODOO_MCP_ALLOW_DIRECT_WRITES 等開關決定，故
# build-once 對不可變容器即足夠（restart 只 re-exec 已 patch 的 disk source，毋須 runtime re-patch）。
# 以 venv python 執行，find_spec 才定位得到。fix_direct_writes 把四個寫入開關的決定與說明接到
# odoo_mcp_admin.write_gates（第三輪：總閘門 ENABLE_WRITES 關＝任何工具都不寫；直接寫入關＝execute_method 不跑
# 未審查方法；chatter 直接張貼關＝execute_method 也不跑在 chatter 張貼的呼叫（message_*、mail.message 等，所以 guard 也傳
# model）；收緊時唯讀只認審過的讀取清單；五支寫入工具的說明與 health_check 第 3 句跟著開關；
# validate_write 回 valid），並讓 health_check 的 runtime 回報 direct_writes_enabled、broad_unknown_method_mode
# 回實際生效的值；它只接受已審查的
# 1.3.0 tools_write.py／write_policy.py 與 token store 修補後的 server_core.py（PINNED，所以必須在上一個修補之後跑）、
# 只寫出 PATCHED，三個檔全有或全無，其他內容 exit 1。
# 2026-10-25（負責人決定，同 HA 0.4.0）：不再套 fix_remove_3step_tools——直接寫入開啟時三支審批工具
# （preview_write／validate_write／execute_approved_write）照樣列出，工具清單維持 41 支。
RUN python apps/odoo/patches/fix_cross_session_token_store.py \
 && python apps/odoo/patches/fix_direct_writes.py \
 && find /app/.venv -type d -name __pycache__ -path '*odoo_mcp*' -exec rm -rf {} +

# 驗證 patch 確實套用：patch 腳本在套件版本漂移致目標字串失配時，可能印 ERROR 卻仍
# exit 0。這裡以 marker 強制檢查，任一缺失即讓 docker build 失敗，避免出「未 patch」的 image；
# 也確認三階段工具的隱藏修補沒有被誤套（PATCH_HIDE_3STEP 不得出現）。
RUN SC="$(python -c 'import importlib.util;print(importlib.util.find_spec("odoo_mcp.server_core").origin)')" \
 && TW="$(python -c 'import importlib.util;print(importlib.util.find_spec("odoo_mcp.tools_write").origin)')" \
 && WP="$(python -c 'import importlib.util;print(importlib.util.find_spec("odoo_mcp.write_policy").origin)')" \
 && grep -q '_GLOBAL_WRITE_APPROVALS' "$SC" \
 && ! grep -q 'PATCH_HIDE_3STEP' "$SC" \
 && grep -q 'PATCH(woowtech): one decision for the four write switches' "$TW" \
 && grep -q 'model, method, safety, lambda: side_effect_method_allowed(model, method), write_switches()' "$TW" \
 && grep -q 'write_gates.execute_method_text(write_switches())' "$TW" \
 && grep -q 'write_gates.chatter_post_text(write_switches())' "$TW" \
 && grep -q 'write_gates.chatter_post_refusal(write_switches())' "$TW" \
 && grep -q 'report\["valid"\] = bool(report.get("success"))' "$TW" \
 && grep -q 'def direct_writes_enabled() -> bool:' "$WP" \
 && grep -q 'def write_switches():' "$WP" \
 && grep -q '"direct_writes_enabled": direct_writes_enabled(),' "$SC" \
 && grep -q 'write_gates.runtime_note(write_switches()),' "$SC" \
 && grep -q 'broad_unknown_in_effect = write_gates.unknown_methods_run(write_switches())' "$SC" \
 && echo "[patch-verify] odoo_mcp patch markers OK" \
 || { echo "[patch-verify] FAILED: odoo_mcp patch markers missing (version drift?)" >&2; exit 1; }
# 修補後的 odoo_mcp 會 import odoo_mcp_admin.write_gates：建置時實際載入一次、41 支工具都註冊得起來（不寫 .pyc，
# 映像內容與之前相同）；載入失敗＝子行程起不來，所以在這裡就讓 build 失敗。
RUN PYTHONDONTWRITEBYTECODE=1 python -c "import asyncio, odoo_mcp.server as s; n = len(asyncio.run(s.mcp.list_tools())); assert n == 41, n; print('[patch-verify] patched odoo_mcp imports, tools:', n)"

COPY --from=frontend-builder /app/apps/odoo/frontend/dist /app/static

# 非 root 執行（woow_paas_platform#1530）：uid／gid 1000 對齊 chart mcp-server 的 fsGroup 1000。
# 程式碼、venv、全域套件維持 root 擁有（app 使用者唯讀），只有 /data（config.json）與 HOME 可寫。
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 1000:1000 /data && chmod 0770 /data
COPY deploy/shared/bootstrap.py /app/bootstrap.py
USER 1000:1000
VOLUME /data
EXPOSE 8080
CMD ["python", "/app/bootstrap.py", "odoo_mcp_admin.main:app"]
