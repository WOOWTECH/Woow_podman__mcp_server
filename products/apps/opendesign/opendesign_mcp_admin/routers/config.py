"""Open Design 連線設定 router（od_api_base／od_chat_timeout／od_basic_user ＋ secret od_basic_password）。

連線鍵名經 mcp_admin_core.process._build_subprocess_env 大寫後注入子行程 env
（``env_prefix=""``）：``od_api_base`` → ``OD_API_BASE``、``od_chat_timeout`` →
``OD_CHAT_TIMEOUT``，1:1 對齊 vendored od_mcp_server.py 模組頂層直讀的固定 env 名，
**故鍵名不可改**（後端 router、前端 appConfig、部署 seed 三處必須同名）。

連線模型（G2-B 已決不設 secret 欄）：
- ``od_api_base``：Open Design daemon URL，非 secret；GET 不遮罩、可清空為 ""（比照 vk）。
- ``od_chat_timeout``：AI 生成最長等待秒數，選配、非 secret、字串化正數；空字串＝**清除**
  （真移除該鍵，讓 vendored server 回退上游預設 300）。⚠️ 不可存 ""——那會令
  _build_subprocess_env 注入 ``OD_CHAT_TIMEOUT=""``，上游 ``float("")`` 會 crash 子行程。
  由於 store.patch 淺合併無法移除鍵，清除分支改走 read-modify-write（store.put 整個
  connection）。

- ``od_basic_user``／``od_basic_password``（#1354）：daemon 前置 basic auth（PaaS 的 open-design
  服務）。password 為 secret：GET 只回遮罩摘要、Save 留空沿用既有值；user 清空＝不帶認證。
  注入子行程 ``OD_BASIC_USER``／``OD_BASIC_PASSWORD``，由 ``opendesign_mcp_server`` 套件
  在 vendored server 讀 ``OD_API_BASE`` 前組進 URL userinfo，httpx 依此自動送 BasicAuth。
  舊做法（帳密直接寫在 URL）仍相容：Save 時把 userinfo 從 od_api_base 拆出、移進這兩欄；
  尚未重存的舊 URL 在 GET 顯示時遮掉密碼。

分層對齊 monorepo 慣例（守 PR #9/#10/#11 canon）：
- Save 一律 strip 前後空白防呆（貼上多帶空白會讓子行程打錯 URL）。
- Save 對 secret 欄（od_basic_password）用 drop_blank_secrets（留空即沿用現值）。
- restart 失敗不讓存檔失敗（partial 語意）：回 success=True/restarted=False 並在 message 說明。

POST /api/config/test（反假 GREEN，G2-A 定案＝A k8s 式誠實分流；讀存檔值、不吃 body）：
- 子行程 running → 走本地 MCP ``tools/call health`` 打通子行程→daemon 全鏈，證
  「橋活著＋整鏈通」（杜絕 daemon 活但橋死的假 GREEN）；
- 子行程未 running → 降級直打 daemon ``/api/health``（health_probes.probe_target，內部
  引用 OD_HEALTH_PATH 單一常數），success 但誠實標「MCP 子行程未執行、未端到端驗證」。
  **嚴禁**改打 supergateway :8000 bridge health——那只證 node 橋活著、OD_API_BASE 錯仍
  回 ok，會假 GREEN（c8c4de3 同型教訓）。

Endpoints:
    GET  /api/config             - 現行連線設定（兩欄皆非 secret、不遮罩）
    PUT  /api/config/connection  - 更新 od_api_base / od_chat_timeout，可選 restart
    POST /api/config/test        - 誠實分流測連線（讀存檔值、不吃 body）
"""
from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome
import logging
import math

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.util.secrets import drop_blank_secrets

from mcp_admin_core.util.mask import mask_secret

from opendesign_mcp_admin import health_probes, mcp_client
from opendesign_mcp_admin.basic_auth import URL_PASSWORD_MASK, redact_url, split_userinfo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConnectionConfig(ConfigurationResponse):
    # od_api_base / od_chat_timeout / od_basic_user 非 secret；od_basic_password 只回遮罩摘要。
    od_api_base: str = ""  # 舊 URL 若仍含 userinfo，密碼以遮罩字樣顯示
    od_chat_timeout: str = ""
    od_basic_user: str = ""
    od_basic_password_masked: str = "(not set)"


class ConnectionUpdateRequest(BaseModel):
    # 沒宣告的欄位（拼錯的鍵）回 422、不再默默忽略；422 不回顯送出的值（core validation_errors）。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    od_api_base: str = Field(default="", description="Open Design daemon base URL (e.g. http://open-design-svc:7457)")
    od_chat_timeout: str | None = Field(
        default=None,
        description="AI 生成最長等待秒數（正數字串；空字串＝清除回退 300；不帶＝不動既有值）",
    )
    od_basic_user: str | None = Field(
        default=None,
        description="Open Design 前置 basic auth 帳號（非 secret；空字串＝不帶認證；不帶＝不動既有值）",
    )
    od_basic_password: str = Field(
        default="",
        description="Open Design 前置 basic auth 密碼（secret；留空＝沿用既有值）",
    )
    restart: bool = Field(default=True, description="更新後是否重啟 MCP 子行程")


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
# GET /api/config
# ---------------------------------------------------------------------------


@router.get("", response_model=ConnectionConfig)
async def get_config(response: Response = None) -> ConnectionConfig:
    """回現行連線設定（密碼只回遮罩摘要；舊 URL userinfo 內的密碼也遮掉）。"""
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get("connection", {})
    password = conn.get("od_basic_password", "")
    return ConnectionConfig(configuration=configuration,
        od_api_base=redact_url(conn.get("od_api_base", "")),
        od_chat_timeout=conn.get("od_chat_timeout", ""),
        od_basic_user=conn.get("od_basic_user", ""),
        od_basic_password_masked=(
            mask_secret(password, prefix=2, suffix=2, min_len=6) if password else "(not set)"
        ),
    )


# ---------------------------------------------------------------------------
# PUT /api/config/connection
# ---------------------------------------------------------------------------


@router.put("/connection", response_model=ConnectionUpdateResponse)
async def update_connection(req: ConnectionUpdateRequest, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> ConnectionUpdateResponse:
    async def apply_update():
        """更新 od_api_base / od_chat_timeout，並視需要重啟 MCP 子行程。"""
        store = get_config_store()

        # 防呆：strip 前後空白。貼上 URL 多帶一個看不見的空白，子行程繼承的 OD_API_BASE
        # 就會打錯 host → daemon 不可達。od_api_base 非 secret，可被有意清空為 ""。
        # #1354：URL 內的 userinfo（舊做法 https://user:pass@host）拆出來移進 basic auth 欄位，
        # URL 欄不再存明文密碼。表單送回 GET 遮罩過的舊 URL（密碼＝遮罩字樣）時視同沒填密碼。
        clean_url, url_user, url_password = split_userinfo(req.od_api_base.strip())
        if url_password == URL_PASSWORD_MASK:
            url_password = None
        updates = {"od_api_base": clean_url}

        basic_user = req.od_basic_user.strip() if req.od_basic_user is not None else None
        if not basic_user and url_user:
            basic_user = url_user
        if basic_user is not None:
            updates["od_basic_user"] = basic_user
        basic_password = req.od_basic_password.strip() or (url_password or "")
        if not basic_password and url_user:
            # 舊 URL 帶 userinfo 但密碼被遮罩／沒給：沿用既有欄位值，再退回存檔 URL 內的密碼。
            existing = await store.get("connection", {})
            if not existing.get("od_basic_password"):
                _, _, stored_url_password = split_userinfo(existing.get("od_api_base", ""))
                basic_password = stored_url_password or ""
        updates["od_basic_password"] = basic_password

        # od_chat_timeout：None＝不帶＝不動既有值；""＝清除；其餘須為正有限數字串。
        clear_timeout = False
        if req.od_chat_timeout is not None:
            t = req.od_chat_timeout.strip()
            if t == "":
                clear_timeout = True  # 清除 → 子行程回退上游預設 300
            else:
                try:
                    val = float(t)
                except ValueError as exc:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"od_chat_timeout 需為正數（秒）：{req.od_chat_timeout!r}",
                    ) from exc
                if not math.isfinite(val) or val <= 0:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        f"od_chat_timeout 需為正的有限數（秒）：{req.od_chat_timeout!r}",
                    )
                updates["od_chat_timeout"] = t  # 存 strip 後原字串（保留 "300"/"300.5"）

        # od_basic_password 留空 → 不覆寫既有存檔值（PR #9/#10/#11 canon）。
        updates = drop_blank_secrets(updates, secret_fields=["od_basic_password"])

        if clear_timeout:
            # store.patch 淺合併無法移除鍵；清除須真移除 od_chat_timeout（否則存 "" → 子行程
            # float("") crash）→ 走 read-modify-write 寫回整個 connection。
            conn = dict(await store.get("connection", {}))
            conn.update(updates)
            conn.pop("od_chat_timeout", None)
            await store.put("connection", conn)
        else:
            await store.patch("connection", updates)
        logger.info("Updated opendesign connection config")

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


# ---------------------------------------------------------------------------
# POST /api/config/test（G2-A：k8s 式誠實分流）
# ---------------------------------------------------------------------------


async def _test_via_mcp(port: int) -> ConnectionTestResponse:
    """路徑一：子行程 running → 走本地 MCP JSON-RPC ``tools/call health``。

    忠實對映子行程實際對話路徑——經子行程走完整協定去 health，回得了 daemon
    的 health 才算「橋活著＋整鏈通」。可達性探測只證 daemon 在線，證不了「橋是否真接通」
    （daemon 活但橋死＝假 GREEN）。
    """
    try:
        result = await mcp_client.call_tool_via_mcp(port, "health", {}, timeout=15.0)
    except mcp_client.McpTimeout:
        return ConnectionTestResponse(
            success=False,
            message=(
                "MCP 橋有回應但 Open Design daemon 未在時限內回應（逾時；與連線設定錯誤不同"
                "——daemon 可能負載過高或網路延遲）。"
            ),
            details={"auth": "mcp_tools_call", "error": "timeout"},
        )
    except mcp_client.McpUnavailable:
        return ConnectionTestResponse(
            success=False,
            message="子行程執行中但 MCP HTTP 尚未就緒（可能剛重啟）——請稍候幾秒再試。",
            details={"auth": "mcp_tools_call", "error": "unavailable"},
        )
    except mcp_client.McpClientError as exc:
        # HTTP listener 已接受連線但回傳無法解析的內容（非 JSON / SSE 無 data 行）——
        # 子行程剛起、MCP 尚未就緒的啟動窗口。既非 timeout 也非 connection-refused，
        # 在此分層誠實回報，別讓 McpClientError 冒成 HTTP 500。
        return ConnectionTestResponse(
            success=False,
            message=f"子行程回應無法解析，可能剛重啟尚未就緒——請稍候幾秒再試（{exc}）。",
            details={"auth": "mcp_tools_call", "error": "unparseable"},
        )

    if result.ok:
        return ConnectionTestResponse(
            success=True,
            message="連線成功：透過 MCP tools/call health 打通 MCP 子行程→Open Design daemon 全鏈，端到端驗證通過。",
            details={"auth": "mcp_tools_call"},
        )
    return ConnectionTestResponse(
        success=False,
        message=f"MCP tools/call health 失敗：{result.message}",
        details={"auth": "mcp_tools_call", "kind": result.kind or "tool_error"},
    )


async def _test_via_reachability(conn: dict) -> ConnectionTestResponse:
    """路徑二：子行程未 running → 降級直打 daemon /api/health（誠實標未端到端驗證）。

    重用 health_probes.probe_target（其內部引用 OD_HEALTH_PATH 單一常數），嚴禁另寫死
    "/api/health"。daemon 無獨立於連線設定的憑證（od_api_token 未設，G2-B），故 /api/health
    可達即代表「連線目標有效」→ success=True；但誠實聲明未經 supergateway 橋端到端驗證。
    """
    od_api_base = conn.get("od_api_base", "")
    if not od_api_base:
        return ConnectionTestResponse(
            success=False,
            message="od_api_base 未設定，無可達性探測目標。",
            details={"auth": "reachability_only"},
        )

    probe = await health_probes.probe_target(conn)
    if probe.get("healthy"):
        return ConnectionTestResponse(
            success=True,
            message=(
                "Open Design daemon /api/health 可達；但 MCP 子行程未執行、未端到端驗證"
                "（未經 MCP 子行程走完整協定鏈，要驗真整鏈需先 Save & Restart 啟動子行程）。"
            ),
            details={
                "auth": "reachability_only",
                "url": probe.get("url"),
                "status_code": probe.get("status_code"),
            },
        )
    return ConnectionTestResponse(
        success=False,
        message=(
            f"MCP 子行程未執行、僅做 daemon 可達性檢查："
            f"{probe.get('error', '不可達')}（未端到端驗證）。"
        ),
        details={"auth": "reachability_only"},
    )


@router.post("/test", response_model=ConnectionTestResponse)
async def test_connection() -> ConnectionTestResponse:
    """Test 一律測存檔值（不吃 body，PR #11 慣例）——就算前端送 body 也被忽略。

    誠實分流（G2-A 定案＝A）：
    - 子行程 running → 走 MCP tools/call health（真端到端，證橋活著＋整鏈通）；
    - 子行程未 running → 降級 daemon /api/health 可達性（success 但明標未端到端驗證）。
    """
    store = get_config_store()
    conn = await store.get("connection", {})

    pm = get_process_manager()
    if pm.is_running:
        mcp_server = await store.get("mcp_server", {})
        port = int(mcp_server.get("port", 8000))
        return await _test_via_mcp(port)
    return await _test_via_reachability(conn)
