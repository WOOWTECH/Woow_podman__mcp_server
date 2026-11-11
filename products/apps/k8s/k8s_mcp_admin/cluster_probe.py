"""叢集可達性探測 helper（Test 路徑二 / health probe 共用）。

**架構鐵律澄清（對齊 plan §2）**：此模組對 target（API server）做的匿名
``GET /version`` 可達性探測**不違反**「admin 不引入 k8s python client／不管理叢集
資源／不自我 rollout」鐵律——這是 target-app 健康檢查慣例（比照 apps/odoo-manage
打 ``/web/health``；k8s app 的 target app 恰好就是 API server）。真正做認證與資源
操作的一律是 MCP 子行程內建的 client-go，admin 只讀 config.json、算出要打哪個 URL、
用什麼 CA，然後發一個無認證的 ``/version`` 探測。

為何自己解析 kubeconfig 而非直接打 healthz：Test/health 要跟子行程「同一份」設定
（current-context 決定的 server + CA）走，才不會 admin 說可達、子行程卻連錯叢集。
httpx 預設 ``verify=True`` 對自簽 CA 會全面 false RED，故 server URL 與 CA 一律
從 current-context 的 cluster 取，insecure-skip-tls-verify 亦忠實反映。
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import ssl
from dataclasses import dataclass

import httpx
import yaml

logger = logging.getLogger(__name__)

# in-cluster 的 SA CA 檔路徑（模組常數，測試可 monkeypatch）。
IN_CLUSTER_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
# in-cluster env 缺 KUBERNETES_SERVICE_HOST 時的預設 API server（叢集內 DNS）。
IN_CLUSTER_DEFAULT_SERVER = "https://kubernetes.default.svc"
# in-cluster 的 SA token 檔路徑（模組常數，測試可 monkeypatch）。#1352：PaaS 部署的 Pod
# automountServiceAccountToken=false（安全上正確），此檔不存在 → in-cluster 必然失敗。
IN_CLUSTER_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
IN_CLUSTER_UNAVAILABLE_MESSAGE = (
    "此環境未掛載叢集權杖（/var/run/secrets/kubernetes.io/serviceaccount/token 不存在，"
    "PaaS 部署預設不掛載），In-Cluster 模式無法使用——請把 In-Cluster 設為 false，"
    "並貼上你自己叢集的 kubeconfig。"
)


def wants_in_cluster(conn: dict) -> bool:
    """connection 是否要求 in-cluster 模式（別名 1/true/yes）。"""
    return str(conn.get("in_cluster", "")).strip().lower() in ("1", "true", "yes")


def in_cluster_available() -> bool:
    """本 Pod 是否掛了 ServiceAccount token（in-cluster 模式的必要條件）。"""
    return os.path.isfile(IN_CLUSTER_TOKEN_PATH)


@dataclass
class ProbeTarget:
    """一次可達性探測的目標描述。

    - ``server``：API server base URL（探測會打 ``{server}/version``）。
    - ``verify``：httpx 的 verify 參數——``False``（insecure-skip-tls-verify）、
      ``True``（用系統信任鏈）、或帶自訂 CA 的 ``ssl.SSLContext``。
    - ``context_name``：current-context 名（in-cluster 固定 "in-cluster"），供 dashboard
      的 db_name 顯示。
    - ``ca_pem``：解碼後的 CA PEM 明文（無自訂 CA 時為 None；供測試檢視與除錯）。
    """

    server: str
    verify: bool | ssl.SSLContext
    context_name: str
    ca_pem: str | None = None


def _build_ssl_context(ca_pem: str) -> ssl.SSLContext:
    """以 CA PEM 明文建立只信任該 CA 的 ssl context（比照上游對自簽叢集 CA 的驗證）。"""
    ctx = ssl.create_default_context(cadata=ca_pem)
    return ctx


def _resolve_in_cluster() -> ProbeTarget:
    """in-cluster 分支：env 組 server、SA ca.crt 當 CA。"""
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    server = f"https://{host}:{port}" if host else IN_CLUSTER_DEFAULT_SERVER

    verify: bool | ssl.SSLContext = True
    ca_pem: str | None = None
    try:
        with open(IN_CLUSTER_CA_PATH, encoding="utf-8") as f:
            ca_pem = f.read()
        verify = _build_ssl_context(ca_pem)
    except OSError:
        # SA CA 檔不在（非叢集內或權限問題）——退回系統信任鏈，不 raise。
        logger.warning("in-cluster CA 檔讀取失敗（%s），退回系統信任鏈", IN_CLUSTER_CA_PATH)

    return ProbeTarget(server=server, verify=verify, context_name="in-cluster", ca_pem=ca_pem)


def _resolve_from_kubeconfig(text: str) -> ProbeTarget:
    """kubeconfig 分支：current-context → cluster 的 server / CA / insecure。

    無 current-context 但恰有一個 context → 自動選（對齊上游 manager.go）；否則 raise
    ValueError（呼叫端翻成 400，防子行程 crash-loop）。
    """
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError("kubeconfig 不是合法的 mapping 結構")

    contexts = doc.get("contexts") or []
    clusters = doc.get("clusters") or []
    if not contexts:
        raise ValueError("kubeconfig 缺少 contexts")
    if not clusters:
        raise ValueError("kubeconfig 缺少 clusters")

    current = doc.get("current-context")
    if not current:
        if len(contexts) == 1:
            current = contexts[0].get("name")
        else:
            raise ValueError("kubeconfig 有多個 context 但未設 current-context")

    ctx_entry = next((c for c in contexts if c.get("name") == current), None)
    if ctx_entry is None:
        raise ValueError(f"current-context {current!r} 在 contexts 中找不到")

    cluster_name = (ctx_entry.get("context") or {}).get("cluster")
    cluster_entry = next((c for c in clusters if c.get("name") == cluster_name), None)
    if cluster_entry is None:
        raise ValueError(f"context {current!r} 指向的 cluster {cluster_name!r} 找不到")

    cluster = cluster_entry.get("cluster") or {}
    server = cluster.get("server")
    if not server:
        raise ValueError(f"cluster {cluster_name!r} 缺少 server")

    verify: bool | ssl.SSLContext = True
    ca_pem: str | None = None
    if cluster.get("insecure-skip-tls-verify"):
        verify = False
    else:
        ca_data = cluster.get("certificate-authority-data")
        if ca_data:
            try:
                ca_pem = base64.b64decode(ca_data).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise ValueError(f"certificate-authority-data 解碼失敗：{exc}") from exc
            verify = _build_ssl_context(ca_pem)

    return ProbeTarget(server=server, verify=verify, context_name=current, ca_pem=ca_pem)


def resolve_target(conn: dict) -> ProbeTarget:
    """自 connection 設定推導探測目標。

    in_cluster=="true" → in-cluster 分支（env + SA CA）；否則解析 kubeconfig。
    kubeconfig 為空且非 in-cluster → raise ValueError（無從探測）。
    """
    if wants_in_cluster(conn):
        return _resolve_in_cluster()

    kubeconfig = str(conn.get("kubeconfig", "")).strip()
    if not kubeconfig:
        raise ValueError("未設定 kubeconfig 且非 in-cluster 模式")
    return _resolve_from_kubeconfig(kubeconfig)


async def fetch_version(
    target: ProbeTarget, *, transport: httpx.BaseTransport | None = None
) -> tuple[bool, str | None, str]:
    """匿名 GET {server}/version 探叢集可達性與版本。

    回傳 ``(reachable, git_version, message)``：
    - 200 → ``(True, gitVersion, …)``。
    - 401/403 → ``(True, None, "匿名 /version 被拒（叢集可達）")``——可達但匿名被擋，
      與「認證失敗」在語意上分開（子行程帶憑證仍可能成功）。
    - 連線／DNS／TLS 錯誤 → ``(False, None, 錯誤訊息)``——不可達，與認證失敗分層表述。

    ``transport`` 僅供測試注入 ``httpx.MockTransport``。timeout 明訂（總 10s、connect 5s）
    避免 dashboard 卡死。
    """
    url = f"{target.server.rstrip('/')}/version"
    timeout = httpx.Timeout(10.0, connect=5.0)
    client_kwargs: dict = {"timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport
    else:
        client_kwargs["verify"] = target.verify

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        logger.warning("叢集可達性探測失敗（%s）：%s", url, exc)
        return False, None, f"無法連上叢集 API server：{exc}"

    if resp.status_code == 200:
        version: str | None = None
        try:
            data = resp.json()
            if isinstance(data, dict):
                version = data.get("gitVersion")
        except ValueError:
            version = None
        return True, version, f"叢集可達（版本 {version or '未知'}）"

    if resp.status_code in (401, 403):
        return True, None, "匿名 /version 被拒（叢集可達）"

    return True, None, f"叢集可達（/version 回應 {resp.status_code}）"
