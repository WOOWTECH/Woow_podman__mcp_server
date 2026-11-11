"""VK Host 連線設定 router（單欄 vibe_backend_url 的讀取與存檔）。

VK Host 現無認證（僅靠 Pod 網路邊界），故連線模型只有一個非 secret 欄
`vibe_backend_url`。鍵名經 mcp_admin_core.process._build_subprocess_env 大寫後
注入子行程 env（vibe_backend_url→VIBE_BACKEND_URL），再由 supergateway 傳遞給
vibe-kanban-mcp 當作 VK Host API 的 backend URL，**故鍵名不可改**（後端 router、
前端 appConfig、部署 seed 三處必須同名）。

分層對齊 monorepo 慣例（比照 apps/odoo-manage，守 PR #9/#10/#11 canon）：
- Save 一律 strip 前後空白防呆（貼上多帶空白會讓子行程打錯 URL）。
- Save 對 secret 欄用 drop_blank_secrets（留空即沿用現值）；VK Host 目前無 secret 欄，
  故 secret_fields 為空 list——此為前瞻掛點，未來加認證欄只需把欄名加進 list。
- restart 失敗不讓存檔失敗（partial 語意）：回 success=True/restarted=False 並在 message 說明。

Test Connection（POST /api/config/test）反假 GREEN 端點選擇：讀 config store 存檔值、
不吃 request body，httpx GET {vibe_backend_url} + VK_HEALTH_PATH 打「vibe-kanban-mcp
子行程實際對話的 VK Host 層」；**嚴禁**改打 supergateway 的 bridge healthz——那只證
node bridge 活著、VIBE_BACKEND_URL 錯仍回 ok，會假 GREEN（c8c4de3 同型教訓）。端點
路徑引用 health_probes.VK_HEALTH_PATH 單一常數（Gate 2 定案 /api/health），詳見
test_connection。

Endpoints:
    GET  /api/config             - 現行連線設定（vibe_backend_url，非 secret 不遮罩）
    PUT  /api/config/connection  - 更新 vibe_backend_url，可選 restart 子行程
    POST /api/config/test        - 測 VK Host /api/health 可達性（讀存檔值、不吃 body）
"""

from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome
import logging

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.util.secrets import drop_blank_secrets

# 端點路徑的單一來源——Test 與 health probe 共用同一常數（Gate 2 定案 /api/health），
# 嚴禁在本檔另寫死 "/api/health" 字串；未來端點若變只改 health_probes 一行。
from vk_mcp_admin.health_probes import VK_HEALTH_PATH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConnectionConfig(ConfigurationResponse):
    # 單一非 secret 欄；GET 不遮罩（比照 secret 才需 mask_secret）
    vibe_backend_url: str = ""


class ConnectionUpdateRequest(BaseModel):
    # 沒宣告的欄位（拼錯的鍵）回 422、不再默默忽略；422 不回顯送出的值（core validation_errors）。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    vibe_backend_url: str = Field(..., description="VK Host API base URL (e.g. http://vk-host:3000)")
    restart: bool = Field(default=True, description="Restart MCP server after update")


class ConnectionUpdateResponse(ConfigurationResponse):
    saved: bool = True
    status: str = "ok"
    apply_state: str = "not_requested"
    success: bool
    message: str
    restarted: bool = False


class ConnectionTestResponse(BaseModel):
    success: bool
    message: str
    details: dict | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=ConnectionConfig)
async def get_config(response: Response = None) -> ConnectionConfig:
    """Return current connection config (vibe_backend_url is not a secret, unmasked)."""
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get("connection", {})
    # vibe_backend_url 非 secret（VK Host 靠 Pod 網路邊界，無 token）→ 原樣回、不遮罩
    return ConnectionConfig(configuration=configuration, vibe_backend_url=conn.get("vibe_backend_url", ""))


@router.put("/connection", response_model=ConnectionUpdateResponse)
async def update_connection(req: ConnectionUpdateRequest, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> ConnectionUpdateResponse:
    async def apply_update():
        """Update VK Host backend URL and optionally restart MCP server."""
        store = get_config_store()

        # 防呆：strip 掉前後空白。貼上 URL 時多帶一個看不見的空白，子行程繼承的
        # VIBE_BACKEND_URL 就會打錯 host → VK Host 不可達。
        updates = {"vibe_backend_url": req.vibe_backend_url.strip()}
        # 目前無 secret 欄，drop_blank_secrets(secret_fields=[]) 為 no-op 前瞻掛點——
        # 未來 VK Host 若加認證欄，只需把欄名加進 secret_fields（PR #9/#10/#11 canon）。
        # vibe_backend_url 非 secret 故不受保護，可被有意清空。
        await store.patch("connection", drop_blank_secrets(updates, secret_fields=[]))
        logger.info("Updated VK connection config")

        applied = False
        started = False
        if req.restart:
            try:
                pm = get_process_manager()
                if pm.is_running:
                    applied = bool(await pm.restart())
                else:
                    started = True
                    applied = bool(await pm.start())
            except Exception:
                # Persistence has already succeeded; do not disguise apply failure as save failure.
                logger.warning("MCP process application failed after saving connection")
        outcome = connection_outcome(req.restart, applied, started=started and applied)
        return ConnectionUpdateResponse(success=True, **outcome)

    return await run_configuration_transition(get_config_store(), get_process_manager(), if_match, apply_update, response, request=request)


@router.post("/test", response_model=ConnectionTestResponse)
async def test_connection() -> ConnectionTestResponse:
    """用存檔的 vibe_backend_url 測 VK Host 可達性（反假 GREEN 核心）。

    (a) 讀 config store 的存檔值、**不吃 request body**——與 MCP server 子行程同一份
        設定來源；函式簽名不收任何 body 參數，就算前端送 body 也一律被忽略。
    (b) httpx GET {vibe_backend_url}{VK_HEALTH_PATH}（= /api/health）打的是 vibe-kanban-mcp
        子行程實際依賴、經 VIBE_BACKEND_URL 對話的 VK Host 層（/api/* 同一 server）；
        HTTP 200 才算「連線設定有效」。VK Host 現無認證，故不帶 header/token。
    (c) **明文禁止**改打 supergateway :8000/healthz——那只證 node bridge 活著，
        VIBE_BACKEND_URL 填錯／VK Host 不可達時 healthz 仍回 ok → 假 GREEN
        （與 odoo-manage c8c4de3 同型教訓）。

    不過度保證：本 Test 只驗 VK Host /api/health 可達性，不宣稱驗證了 MCP 協定或工具鏈。
    """
    store = get_config_store()
    conn = await store.get("connection", {})

    vibe_backend_url = conn.get("vibe_backend_url", "")
    if not vibe_backend_url:
        return ConnectionTestResponse(
            success=False,
            message="vibe_backend_url is not configured",
        )

    # 引用 health_probes.VK_HEALTH_PATH 單一常數（與 probe 同源），嚴禁另寫死 "/api/health"。
    url = f"{vibe_backend_url.rstrip('/')}{VK_HEALTH_PATH}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.ConnectError as exc:
        return ConnectionTestResponse(success=False, message=f"Connection refused: {exc}")
    except httpx.TimeoutException:
        return ConnectionTestResponse(success=False, message="Connection timed out after 10 s")
    except httpx.HTTPStatusError as exc:
        # 帶狀態碼與截斷 body，方便前端顯示 VK Host 回的錯誤
        return ConnectionTestResponse(
            success=False,
            message=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
        )
    except Exception as exc:  # catch-all：非預期例外轉可讀訊息，不讓 Test 拋 500
        return ConnectionTestResponse(success=False, message=f"Unexpected error: {exc}")

    return ConnectionTestResponse(
        success=True,
        message="VK Host API is reachable",
        details={"url": url, "status_code": resp.status_code},
    )
