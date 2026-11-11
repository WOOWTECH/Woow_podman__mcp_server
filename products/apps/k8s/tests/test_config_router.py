"""config router（GET /api/config、PUT /api/config/connection）的契約測試。

connection 僅 kubeconfig（textarea secret）＋ in_cluster 兩欄：
- GET 回摘要遮罩（"(set, 3.2 KB)" / "(not set)"），不外洩 kubeconfig 原文。
- PUT 做 in_cluster 正規化（別名→true/false，非法→400）、in-cluster 主動清空
  kubeconfig、kubeconfig yaml 結構驗證（防子行程 crash-loop）、strip + drop_blank_secrets。
"""
from __future__ import annotations

from connection_forms import assert_unknown_field_rejected, gui_connection_fields
from management_client import revision_headers

import yaml

from tests.conftest import stored_connection

# 合法最小 kubeconfig（clusters + contexts + current-context）。
_MIN_KUBECONFIG = yaml.safe_dump(
    {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c1", "cluster": {"server": "https://api:6443"}}],
        "contexts": [{"name": "ctx1", "context": {"cluster": "c1", "user": "u"}}],
        "current-context": "ctx1",
        "users": [{"name": "u", "user": {}}],
    }
)


class _FakePM:
    def __init__(self, running: bool, restart_raises: bool = False, start_ok: bool = True):
        self._running = running
        self._restart_raises = restart_raises
        self._start_ok = start_ok
        self.restart_called = False
        self.start_called = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def restart(self) -> bool:
        self.restart_called = True
        if self._restart_raises:
            raise RuntimeError("boom")
        return True

    async def start(self) -> bool:
        self.start_called = True
        return self._start_ok


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


def test_get_config_masked_summary(make_client):
    kubeconfig = "x" * 3300  # 3300 bytes → 3.2 KB
    client = make_client(connection={"kubeconfig": kubeconfig, "in_cluster": "false"})

    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()

    assert body["kubeconfig_masked"].startswith("(set,")
    assert "3.2 KB" in body["kubeconfig_masked"]
    assert body["in_cluster"] == "false"
    # 不得外洩原文
    assert "xxxx" not in resp.text


def test_get_config_not_set(make_client):
    client = make_client(connection={"in_cluster": "false"})

    body = client.get("/api/config").json()
    assert body["kubeconfig_masked"] == "(not set)"


# ---------------------------------------------------------------------------
# PUT /api/config/connection
# ---------------------------------------------------------------------------


def test_put_connection_valid_kubeconfig_saved(make_client, config_path):
    client = make_client(connection={"in_cluster": "false"})

    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false", "restart": False},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200

    conn = stored_connection(config_path)
    # PUT 對 kubeconfig 做 strip 防呆（前後空白），故比對 strip 後的值
    assert conn["kubeconfig"] == _MIN_KUBECONFIG.strip()
    assert isinstance(conn["kubeconfig"], str)


def test_put_connection_blank_kubeconfig_preserved(make_client, config_path):
    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})

    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": "", "in_cluster": "false", "restart": False},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200

    conn = stored_connection(config_path)
    assert conn["kubeconfig"] == _MIN_KUBECONFIG  # 留空沿用


def test_put_connection_in_cluster_true_clears_kubeconfig(make_client, config_path):
    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})

    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": "", "in_cluster": "true", "restart": False},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200

    conn = stored_connection(config_path)
    assert conn["kubeconfig"] == ""  # 主動清空＝憑證撤下
    assert conn["in_cluster"] == "true"


def test_put_connection_invalid_yaml_400(make_client):
    client = make_client(connection={"in_cluster": "false"})

    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": "key: [unclosed", "in_cluster": "false", "restart": False},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 400
    assert resp.json()["detail"]


def test_put_connection_missing_clusters_400(make_client):
    client = make_client(connection={"in_cluster": "false"})
    bad = yaml.safe_dump({"apiVersion": "v1", "contexts": [{"name": "ctx1", "context": {}}]})

    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": bad, "in_cluster": "false", "restart": False},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 400


def test_put_connection_multi_context_no_current_400(make_client):
    client = make_client(connection={"in_cluster": "false"})
    bad = yaml.safe_dump(
        {
            "apiVersion": "v1",
            "clusters": [{"name": "c1", "cluster": {"server": "https://a"}}],
            "contexts": [
                {"name": "ctx1", "context": {"cluster": "c1"}},
                {"name": "ctx2", "context": {"cluster": "c1"}},
            ],
        }
    )

    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": bad, "in_cluster": "false", "restart": False},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 400


def test_put_connection_in_cluster_alias_normalized(make_client, config_path):
    client = make_client(connection={"in_cluster": "false"})

    # "1" → "true"
    client.put("/api/config/connection", json={"in_cluster": "1", "restart": False}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_connection(config_path)["in_cluster"] == "true"

    # "no" → "false"
    client.put("/api/config/connection", json={"in_cluster": "no", "restart": False}, headers=revision_headers(client, None, read_path='/api/config'))
    assert stored_connection(config_path)["in_cluster"] == "false"

    # "maybe" → 400
    resp = client.put("/api/config/connection", json={"in_cluster": "maybe", "restart": False}, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 400


def test_put_connection_restart_semantics(make_client, monkeypatch):
    from k8s_mcp_admin.routers import config as config_mod

    # running → restart 被呼叫
    pm = _FakePM(running=True)
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm)
    client = make_client(connection={"in_cluster": "false"})
    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false", "restart": True},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200
    assert resp.json()["restarted"] is True
    assert pm.restart_called is True

    # restart raise → 200 + partial 語意（已存檔、重啟失敗）
    pm2 = _FakePM(running=True, restart_raises=True)
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm2)
    resp2 = client.put(
        "/api/config/connection",
        json={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false", "restart": True},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["success"] is True
    assert body["restarted"] is False
    assert "重啟失敗" in body["message"]


def test_put_connection_starts_process_when_not_running(make_client, monkeypatch):
    """#1350：開機因缺 kubeconfig 而 exit 的子行程，存設定後要被 start（不只 restart）。"""
    from k8s_mcp_admin.routers import config as config_mod

    pm = _FakePM(running=False)
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm)
    client = make_client(connection={"in_cluster": "false"})
    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false", "restart": True},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200
    body = resp.json()
    assert pm.start_called is True
    assert pm.restart_called is False
    assert body["restarted"] is True
    assert "已啟動" in body["message"]

    # start 失敗 → 仍 200、已存檔，訊息指向 Settings 的 MCP 狀態
    pm2 = _FakePM(running=False, start_ok=False)
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm2)
    resp2 = client.put(
        "/api/config/connection",
        json={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false", "restart": True},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp2.status_code == 200
    assert resp2.json()["restarted"] is False
    assert "啟動失敗" in resp2.json()["message"]


# ---------------------------------------------------------------------------
# 沒宣告的欄位回 422、不默默忽略（以前回 200 什麼都沒存；MCP:Hermes 2026-11-05 同型）
# ---------------------------------------------------------------------------

_SECRET = "k8s-canary-3c9d71-secret"


def test_put_connection_gui_payload_accepted(make_client, config_path, monkeypatch):
    """Connection 頁送 kubeconfig／in_cluster 兩欄（in_cluster 是文字欄、送字串）、不送 restart（預設重啟）。"""
    from k8s_mcp_admin.routers import config as config_mod

    pm = _FakePM(running=True)
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm)
    client = make_client(connection={"in_cluster": "false"})
    gui = {"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"}
    assert set(gui) == gui_connection_fields("k8s")
    resp = client.put("/api/config/connection", json=gui, headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200, resp.text
    assert stored_connection(config_path) == {"kubeconfig": _MIN_KUBECONFIG.strip(), "in_cluster": "false"}
    assert pm.restart_called is True


def test_put_connection_misspelled_field_rejected_without_echo_or_effect(make_client, config_path, monkeypatch):
    """kube_config（拼錯）帶著含 token 的 kubeconfig：422、不回顯 token、不存檔也不重啟。"""
    from k8s_mcp_admin.routers import config as config_mod

    pm = _FakePM(running=True)
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm)
    client = make_client(connection={"in_cluster": "false"})
    leaked = _MIN_KUBECONFIG.replace("user: {}", f"user: {{token: {_SECRET}}}")
    assert _SECRET in leaked
    headers = revision_headers(client, None, read_path='/api/config')
    before = config_path.read_bytes()
    resp = client.put("/api/config/connection", json={"kube_config": leaked, "in_cluster": "false"}, headers=headers)
    assert_unknown_field_rejected(resp, "kube_config", _SECRET)
    assert config_path.read_bytes() == before
    assert pm.restart_called is False and pm.start_called is False


# ---------------------------------------------------------------------------
# POST /api/config/test（混合分流：running 走 MCP tools/call、未 running 降級 reachability）
# ---------------------------------------------------------------------------


def _patch_pm(monkeypatch, running: bool):
    """monkeypatch config 模組的 get_process_manager，回傳固定 is_running 的 FakePM。"""
    from k8s_mcp_admin.routers import config as config_mod

    pm = _FakePM(running=running)
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm)
    return pm


def test_test_running_success(make_client, monkeypatch):
    from k8s_mcp_admin import mcp_client

    _patch_pm(monkeypatch, running=True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        assert tool == "namespaces_list"
        return mcp_client.McpCallResult(ok=True, message="default\nkube-system")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)

    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    body = client.post("/api/config/test").json()

    assert body["success"] is True
    assert "namespaces_list" in body["message"]
    assert body["details"]["auth"] == "mcp_tools_call"


def test_test_running_rbac_limited(make_client, monkeypatch):
    from k8s_mcp_admin import mcp_client

    _patch_pm(monkeypatch, running=True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        return mcp_client.McpCallResult(ok=False, kind="rbac", message="namespaces is forbidden")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)

    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    body = client.post("/api/config/test").json()

    assert body["success"] is True
    assert "已連上、憑證有效但 RBAC 受限" in body["message"]


def test_test_running_timeout(make_client, monkeypatch):
    from k8s_mcp_admin import mcp_client

    _patch_pm(monkeypatch, running=True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        raise mcp_client.McpTimeout("timed out")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)

    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    body = client.post("/api/config/test").json()

    assert body["success"] is False
    assert "MCP server 有回應但叢集 API 未在時限內回應" in body["message"]


def test_test_running_but_port_refused(make_client, monkeypatch):
    from k8s_mcp_admin import mcp_client

    _patch_pm(monkeypatch, running=True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        raise mcp_client.McpUnavailable("connection refused")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)

    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    resp = client.post("/api/config/test")
    # 不得冒成 HTTP 500
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "子行程執行中但 MCP HTTP 尚未就緒（可能剛重啟）" in body["message"]


def test_test_running_but_malformed_response(make_client, monkeypatch):
    from k8s_mcp_admin import mcp_client

    _patch_pm(monkeypatch, running=True)

    async def _fake_call(port, tool, arguments, timeout=15.0, transport=None):
        # 子行程 HTTP listener 已接受連線但回傳無法解析的內容（剛重啟尚未就緒）——
        # call_tool_via_mcp 會拋基礎 McpClientError（既非 timeout 也非 unavailable）。
        raise mcp_client.McpClientError("回應非合法 JSON")

    monkeypatch.setattr(mcp_client, "call_tool_via_mcp", _fake_call)

    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    resp = client.post("/api/config/test")
    # 不得以 unhandled exception 冒成 HTTP 500
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "無法解析" in body["message"]
    assert body["details"]["auth"] == "mcp_tools_call"


def test_test_not_running_degrades_to_reachability(make_client, monkeypatch):
    from k8s_mcp_admin import cluster_probe

    _patch_pm(monkeypatch, running=False)

    async def _fake_fetch(target, *, transport=None):
        return True, "v1.31.0", "叢集可達（版本 v1.31.0）"

    monkeypatch.setattr(cluster_probe, "fetch_version", _fake_fetch)

    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    body = client.post("/api/config/test").json()

    assert body["success"] is False  # 不宣稱認證通過
    assert "MCP server 未執行" in body["message"]
    assert "v1.31.0" in body["message"]
    assert body["details"]["auth"] == "reachability_only"
    assert body["server_version"] == "v1.31.0"


def test_test_not_running_unreachable(make_client, monkeypatch):
    from k8s_mcp_admin import cluster_probe

    _patch_pm(monkeypatch, running=False)

    async def _fake_fetch(target, *, transport=None):
        return False, None, "無法連上叢集 API server：Connection refused"

    monkeypatch.setattr(cluster_probe, "fetch_version", _fake_fetch)

    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    body = client.post("/api/config/test").json()

    assert body["success"] is False
    assert "無法連上叢集 API server" in body["message"]
    assert body["details"]["auth"] == "reachability_only"


def test_test_no_config_at_all(make_client, monkeypatch):
    _patch_pm(monkeypatch, running=False)

    client = make_client(connection={"in_cluster": "false"})
    body = client.post("/api/config/test").json()

    assert body["success"] is False
    assert "未設定 kubeconfig（子行程將 auto-detect）" in body["message"]
    assert body["details"]["auth"] == "reachability_only"
