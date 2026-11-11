"""K8s 連線設定 router（connection 僅 kubeconfig ＋ in_cluster 兩欄）。

連線鍵名經 mcp_admin_core.process._build_subprocess_env 大寫後加 ``env_prefix="K8S_MCP_"``
注入子行程 env：``kubeconfig`` → ``K8S_MCP_KUBECONFIG``、``in_cluster`` →
``K8S_MCP_IN_CLUSTER``（launcher 據此派生 /data/kubeconfig 與 in-cluster 分支），故
**鍵名不可改**。

分層對齊 monorepo 慣例（比照 apps/odoo-manage）：
- GET 回摘要遮罩（kubeconfig 常等於 cluster-admin 憑證，絕不回原文；只回長度摘要）。
- PUT 對 kubeconfig 做 yaml 結構驗證——0/多個 context 且無 current-context 會讓上游
  Go 子行程啟動報錯（crash-loop），故在 API 邊界先擋（回 400），別讓壞設定落檔。
- in_cluster 在 API 邊界正規化（別名 1/yes/no… → "true"/"false"，非法值 400）。
- in_cluster="true" 存檔時**主動清空 kubeconfig**（憑證撤下機制，決策 3；切回需重貼）。
- secret 欄（kubeconfig）留空即沿用（drop_blank_secrets + store.patch 淺合併）。

Endpoints:
    GET  /api/config             - 現行 connection（kubeconfig 摘要遮罩）
    PUT  /api/config/connection  - 更新 kubeconfig / in_cluster
    （POST /api/config/test 由 Task 14 補上）
"""
from __future__ import annotations

from typing import Annotated
from fastapi import Request, Header, Response
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status, revision_header, run_configuration_transition)

from mcp_admin_core.connection_apply import connection_outcome
import logging
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.util.secrets import drop_blank_secrets

from k8s_mcp_admin import cluster_probe, mcp_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_in_cluster(raw: str) -> str:
    """in_cluster 別名正規化到 {"true","false"}；非法值原樣回（呼叫端判 400）。"""
    v = (raw or "").strip().lower()
    if v in ("1", "true", "yes"):
        return "true"
    if v in ("", "0", "false", "no"):
        return "false"
    return v


def _validate_kubeconfig(text: str) -> None:
    """驗證 kubeconfig 結構，不合法即 raise HTTPException(400)。

    擋掉會讓上游 Go 子行程啟動失敗的形態：非合法 YAML、非 mapping、缺 clusters/contexts、
    多個 context 但未設 current-context（上游此時無從決定 current-context → 啟動報錯）。
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"kubeconfig 不是合法 YAML：{exc}"
        ) from exc

    if not isinstance(doc, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kubeconfig 結構不是 mapping")

    if not doc.get("clusters"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kubeconfig 缺少非空 clusters")
    contexts = doc.get("contexts")
    if not contexts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kubeconfig 缺少非空 contexts")

    if len(contexts) > 1 and not doc.get("current-context"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "多個 context 但未設 current-context（子行程會啟動失敗）",
        )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConnectionConfig(ConfigurationResponse):
    kubeconfig_masked: str = "(not set)"
    in_cluster: str = "false"


class ConnectionUpdateRequest(BaseModel):
    # 沒宣告的欄位（拼錯的鍵）回 422、不再默默忽略；422 不回顯送出的值（core validation_errors）。
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kubeconfig: str = Field(default="", description="kubeconfig YAML 全文（留空沿用現值）")
    in_cluster: str = Field(default="false", description="in-cluster 模式：true/false（接受別名）")
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
    server_version: str | None = None
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=ConnectionConfig)
async def get_config(response: Response = None) -> ConnectionConfig:
    """回現行 connection（kubeconfig 只回長度摘要，不回原文）。"""
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get("connection", {})
    kubeconfig = conn.get("kubeconfig", "")
    if kubeconfig:
        kb = len(kubeconfig.encode("utf-8")) / 1024
        masked = f"(set, {kb:.1f} KB)"
    else:
        masked = "(not set)"
    return ConnectionConfig(configuration=configuration,
        kubeconfig_masked=masked,
        in_cluster=conn.get("in_cluster", "false"),
    )


@router.put("/connection", response_model=ConnectionUpdateResponse)
async def update_connection(req: ConnectionUpdateRequest, response: Response = None, if_match: Annotated[str | None, Header(alias="If-Match")] = None, request: Request = None) -> ConnectionUpdateResponse:
    async def apply_update():
        """更新 kubeconfig / in_cluster，並視需要重啟 MCP 子行程。"""
        store = get_config_store()

        normalized = _normalize_in_cluster(req.in_cluster)
        if normalized not in ("true", "false"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid in_cluster value: {req.in_cluster!r}. "
                "Must normalize to true / false (aliases like 1/yes/no accepted).",
            )

        if normalized == "true" and not cluster_probe.in_cluster_available():
            # #1352：沒掛 SA token（PaaS 部署 automountServiceAccountToken=false）時 in-cluster 必然
            # 失敗；而切 in-cluster 會清空已存檔 kubeconfig——在邊界擋下，別讓租戶把可用設定換成壞設定。
            raise HTTPException(status.HTTP_400_BAD_REQUEST, cluster_probe.IN_CLUSTER_UNAVAILABLE_MESSAGE)

        if normalized == "true":
            # 切 in-cluster：主動清空 kubeconfig（顯式，繞過 drop_blank_secrets）＝憑證撤下。
            updates: dict = {"kubeconfig": "", "in_cluster": "true"}
        else:
            kubeconfig = req.kubeconfig.strip()
            if kubeconfig:
                _validate_kubeconfig(kubeconfig)  # 不合法 → 400，別讓壞設定落檔
            # 留空的 kubeconfig 交給 drop_blank_secrets 移除 → store.patch 淺合併沿用現值。
            updates = drop_blank_secrets(
                {"kubeconfig": kubeconfig, "in_cluster": normalized}, ["kubeconfig"]
            )

        await store.patch("connection", updates)
        logger.info("Updated k8s connection config (in_cluster=%s)", normalized)

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
# POST /api/config/test（混合分流）
# ---------------------------------------------------------------------------


async def _test_via_mcp(port: int) -> ConnectionTestResponse:
    """路徑一：子行程 running → 走 MCP JSON-RPC tools/call namespaces_list。

    namespaces_list 屬 core toolset，toolsets 固定於 seed 故依賴穩定；徒手改
    K8S_MCP_TOOLSETS 拿掉 core 會使本 Test 失效（README 註明）。此路徑忠實對映子行程
    client-go 的認證路徑——讓子行程自己帶 kubeconfig 憑證去 tools/call，回得了
    namespaces 才算「認證真的通得過」（可達性探測只證叢集在線、證不了憑證，見 PR #14
    假 GREEN 教訓）。
    """
    try:
        result = await mcp_client.call_tool_via_mcp(port, "namespaces_list", {}, timeout=15.0)
    except mcp_client.McpTimeout:
        return ConnectionTestResponse(
            success=False,
            message=(
                "MCP server 有回應但叢集 API 未在時限內回應（逾時；與認證失敗不同——"
                "叢集 API server 可能負載過高或網路延遲）。"
            ),
            details={"auth": "mcp_tools_call", "error": "timeout"},
        )
    except mcp_client.McpUnavailable:
        return ConnectionTestResponse(
            success=False,
            message=(
                "子行程執行中但 MCP HTTP 尚未就緒（可能剛重啟）——請稍候幾秒再試。"
            ),
            details={"auth": "mcp_tools_call", "error": "unavailable"},
        )
    except mcp_client.McpClientError as exc:
        # HTTP listener 已接受連線但回傳無法解析的內容（非合法 JSON / SSE 無 data 行 /
        # HTML 錯誤頁）——Go binary 的 HTTP 尚起、MCP 尚未就緒的啟動窗口。既非 timeout
        # 也非 connection-refused，故不會被上兩個分支接住；在此分層誠實回報，別讓
        # McpClientError 冒成 HTTP 500（訊息分層誠實原則）。
        return ConnectionTestResponse(
            success=False,
            message=(
                f"子行程回應無法解析，可能剛重啟尚未就緒——請稍候幾秒再試（{exc}）。"
            ),
            details={"auth": "mcp_tools_call", "error": "unparseable"},
        )

    if result.ok:
        return ConnectionTestResponse(
            success=True,
            message="連線成功：透過 MCP tools/call namespaces_list 取得叢集 namespaces，憑證有效。",
            details={"auth": "mcp_tools_call"},
        )
    if result.kind == "rbac":
        return ConnectionTestResponse(
            success=True,
            message=(
                "已連上、憑證有效但 RBAC 受限：tools/call namespaces_list 被拒（403）。"
                f"子行程回應：{result.message}"
            ),
            details={"auth": "mcp_tools_call", "kind": "rbac"},
        )
    return ConnectionTestResponse(
        success=False,
        message=f"MCP tools/call namespaces_list 失敗：{result.message}",
        details={"auth": "mcp_tools_call", "kind": result.kind or "tool_error"},
    )


async def _test_via_reachability(conn: dict) -> ConnectionTestResponse:
    """路徑二：子行程未 running → 降級為叢集可達性檢查（不宣稱認證通過）。

    可達性探測只證叢集 API 在線（匿名 GET /version），證不了憑證有效，故一律回
    success=False——要驗真認證需先啟動子行程走路徑一。
    """
    prefix = "MCP server 未執行，僅做叢集可達性檢查："
    try:
        target = cluster_probe.resolve_target(conn)
    except ValueError as exc:
        # 未設定 kubeconfig 且非 in-cluster：無探測目標，誠實說明子行程屆時會自行 auto-detect。
        return ConnectionTestResponse(
            success=False,
            message=(
                f"{prefix}未設定 kubeconfig（子行程將 auto-detect）——會由子行程自行嘗試 "
                f"~/.kube/config 或 in-cluster；目前無可達性探測目標（{exc}）。"
            ),
            details={"auth": "reachability_only"},
        )

    reachable, version, msg = await cluster_probe.fetch_version(target)
    if reachable:
        return ConnectionTestResponse(
            success=False,  # 僅可達、未驗憑證
            message=(
                f"{prefix}{msg}。注意：僅證叢集 API 可達，尚未驗證憑證——"
                "需啟動子行程才能做真認證。"
            ),
            server_version=version,
            details={"auth": "reachability_only"},
        )
    return ConnectionTestResponse(
        success=False,
        message=f"{prefix}{msg}",
        details={"auth": "reachability_only"},
    )


@router.post("/test", response_model=ConnectionTestResponse)
async def test_connection() -> ConnectionTestResponse:
    """Test 一律測存檔值（不吃 body，PR #11 慣例）——連線表單 kubeconfig 欄遮罩顯示，
    前端送出只會是遮罩值必定失敗；測存檔值才可靠。

    混合分流（訊息分層誠實）：
    - 子行程 running → 走 MCP tools/call namespaces_list（真端到端，讓 client-go 帶憑證
      去驗；假 GREEN 教訓 PR #14：k8s 唯一不漂移的路是讓子行程自己驗）。
    - 子行程未 running → 降級叢集可達性檢查（匿名 GET /version），回 success=False 並
      明說「未驗憑證」，別讓可達假裝成認證通過。
    """
    store = get_config_store()
    conn = await store.get("connection", {})

    if cluster_probe.wants_in_cluster(conn) and not cluster_probe.in_cluster_available():
        # #1352：in-cluster 但環境沒掛 SA token → 子行程必然 exit 1，直接回可讀原因。
        return ConnectionTestResponse(
            success=False,
            message=cluster_probe.IN_CLUSTER_UNAVAILABLE_MESSAGE,
            details={"auth": "in_cluster_unavailable"},
        )

    pm = get_process_manager()
    if pm.is_running:
        mcp_server = await store.get("mcp_server", {})
        port = int(mcp_server.get("port", 8000))
        return await _test_via_mcp(port)
    return await _test_via_reachability(conn)
