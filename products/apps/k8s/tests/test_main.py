"""main.py 組裝的 smoke 測試：core factory 掛載 + /healthz + 各 router 路徑存在。

不進 lifespan（不啟動子行程）：只驗 app 組裝正確、/healthz 恆 200、四個 extra router
的關鍵路徑都掛上了。
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_ok_without_config(config_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod

    from k8s_mcp_admin.main import app

    # 「沒有設定檔」＝tmp 路徑下尚未建立；不可落到預設的 /data/config.json（非 root 的 CI 容器沒有 /data 也建不了）。
    assert not config_path.exists()
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))
    monkeypatch.setattr(store_mod, "_instance", None)

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    # mcp：給平台的粗略狀態（k8s 不宣告必要欄位；這裡沒有子行程）
    assert resp.json() == {"status": "ok", "mcp": "not_running"}


def test_healthz_has_no_backend_field_even_without_kubeconfig(config_path, monkeypatch):
    """k8s 不宣告後端連線欄位（沒有 kubeconfig 上游就起不來，屬於「必要」、而且是 kubeconfig 或 in-cluster 的條件式需求），
    所以 /healthz 只有 status 與 mcp、沒有 backend（第五輪）。"""
    import json

    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

    from k8s_mcp_admin.main import LAUNCH_POLICY, app

    assert LAUNCH_POLICY.backend_connection == () and LAUNCH_POLICY.required_connection == ()
    cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    cfg["connection"] = {"kubeconfig": "", "in_cluster": "false"}
    config_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    monkeypatch.setattr(process_mod, "_instance", None)
    previous = get_launch_policy()
    set_launch_policy(LAUNCH_POLICY)
    try:
        resp = TestClient(app).get("/healthz")
    finally:
        set_launch_policy(previous)
    assert resp.status_code == 200 and resp.json() == {"status": "ok", "mcp": "not_running"}


def _collect_paths(routes) -> set[str]:
    """收集所有路由 path，含 include_router 產生的 _IncludedRouter（新版 Starlette 把
    掛入的 router 收攏成 _IncludedRouter，真正的子路由掛在 original_router.routes 下）。"""
    paths: set[str] = set()
    for r in routes:
        p = getattr(r, "path", None)
        if p:
            paths.add(p)
        orig = getattr(r, "original_router", None)
        if orig is not None:
            paths |= _collect_paths(orig.routes)
    return paths


def test_routes_mounted():
    from k8s_mcp_admin.main import app

    paths = _collect_paths(app.routes)
    for expected in (
        "/api/config",
        "/api/config/connection",
        "/api/config/test",
        "/api/tools",
        "/api/health",
        "/api/logs/stream",
    ):
        assert expected in paths, f"缺少路由 {expected}；已掛載：{sorted(p for p in paths if p)}"


def test_fresh_install_config_is_all_on(tmp_path, monkeypatch):
    """沒有部署 seed、由本行程建立 config.json（全新安裝）：mcp_server.env 是全開的治理開關。"""
    import json

    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

    from k8s_mcp_admin.launcher import FRESH_ENV
    from k8s_mcp_admin.main import LAUNCH_POLICY

    saved = get_launch_policy()
    set_launch_policy(LAUNCH_POLICY)
    try:
        path = tmp_path / "config.json"
        store_mod.ConfigStore(path=path)
        env = json.loads(path.read_text())["mcp_server"]["env"]
    finally:
        set_launch_policy(saved)
    assert env == FRESH_ENV and env["K8S_MCP_MULTI_CLUSTER"] == "1"
    assert dict(LAUNCH_POLICY.fresh_env) == FRESH_ENV
    assert set(FRESH_ENV) <= LAUNCH_POLICY.editable_env_keys
