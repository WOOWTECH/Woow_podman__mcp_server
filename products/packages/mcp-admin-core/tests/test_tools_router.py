"""tools factory 契約測試：GET 形狀、PUT 三 payload、/operations 驗證、mcp_label。"""
from __future__ import annotations

from management_client import revision_headers

from enum import Enum
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp_admin_core.operation_policy import OperationSpec


class _Cat(str, Enum):
    A = "CatA"
    B = "CatB"


def _tool(name, cat, ops, dangerous=False):
    return SimpleNamespace(name=name, category=cat, description=f"{name} desc",
                           operations=ops, dangerous=dangerous)


def _fake_registry():
    reg = [
        _tool("alpha", _Cat.A, ["read", "write"]),
        _tool("beta", _Cat.B, []),            # odoo 式：無 operations
    ]
    names = [t.name for t in reg]
    return SimpleNamespace(
        TOOL_REGISTRY=reg,
        OPERATION_POLICIES={"alpha": OperationSpec(("read", "write"), "action", "read", normalization="lower", empty_uses_default=True)},
        ToolCategory=_Cat,
        get_all_tool_names=lambda: names,
        get_tool_by_name=lambda n: next((t for t in reg if t.name == n), None),
    )


def _client(mcp_label="fake-mcp", operations_supported=True, registry=None):
    from mcp_admin_core.routers.tools import create_tools_router
    app = FastAPI()
    app.include_router(create_tools_router(registry=registry or _fake_registry(), mcp_label=mcp_label,
                                           operations_supported=operations_supported))
    return TestClient(app)


class _FakePM:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    async def start_or_restart(self):
        self.calls += 1
        return self.ok


def test_get_tools_shape_includes_empty_category_buckets():
    c = _client()
    body = c.get("/api/tools").json()
    assert body["total"] == 2
    assert body["enabled"] == 2 and body["disabled"] == 0
    assert set(body["categories"]) == {"CatA", "CatB"}      # 含所有 category 桶
    assert {t["name"] for t in body["tools"]} == {"alpha", "beta"}
    assert all(t["operation_note"] is None for t in body["tools"])  # registry 沒宣告 OPERATION_NOTES


def test_registry_operation_notes_are_served_next_to_their_tools():
    """產品覆核過的說明（registry 的 OPERATION_NOTES）原樣出現在 /api/tools 對應工具的 operation_note。"""
    registry = _fake_registry()
    registry.OPERATION_NOTES = {"alpha": "alpha 的限制擋不住另一支工具", "missing": "沒有這支工具"}
    body = _client(registry=registry).get("/api/tools").json()
    notes = {t["name"]: t["operation_note"] for t in body["tools"]}
    assert notes == {"alpha": "alpha 的限制擋不住另一支工具", "beta": None}


def test_put_list_payload_disables(store_env):
    c = _client()
    r = c.put("/api/tools", json={"tools": [{"name": "beta", "enabled": False}]}, headers=revision_headers(c, None, read_path='/api/tools'))
    assert r.status_code == 200
    assert r.json()["disabled_tools"] == ["beta"]


def test_put_dict_payload_disables(store_env):
    c = _client()
    r = c.put("/api/tools", json={"tools": {"alpha": False, "beta": True}}, headers=revision_headers(c, None, read_path='/api/tools'))
    assert r.json()["disabled_tools"] == ["alpha"]


def test_operations_unknown_tool_400():
    c = _client()
    r = c.put("/api/tools/operations", json={"disabled_operations": {"ghost": ["x"]}}, headers=revision_headers(c, None, read_path='/api/tools'))
    assert r.status_code == 400


def test_operations_unknown_op_400():
    c = _client()
    r = c.put("/api/tools/operations", json={"disabled_operations": {"alpha": ["nope"]}}, headers=revision_headers(c, None, read_path='/api/tools'))
    assert r.status_code == 400


def test_operations_success_message_has_mcp_label(store_env, monkeypatch):
    from mcp_admin_core.routers import tools as tools_mod

    pm = _FakePM()
    monkeypatch.setattr(tools_mod, "get_process_manager", lambda: pm)
    c = _client(mcp_label="odoo-mcp")
    r = c.put("/api/tools/operations", json={"disabled_operations": {"alpha": ["READ", "read", " "]}}, headers=revision_headers(c, None, read_path='/api/tools'))
    assert r.status_code == 200
    assert "odoo-mcp" in r.json()["message"]
    # 上游比對一律小寫：存檔正規化＋去重；子行程重啟（或拉起）才會讀到新的 env
    assert r.json()["disabled_operations"] == {"alpha": ["read"]}
    assert pm.calls == 1


def test_operations_not_running_reports_pending(store_env, monkeypatch):
    from mcp_admin_core.routers import tools as tools_mod

    monkeypatch.setattr(tools_mod, "get_process_manager", lambda: _FakePM(ok=False))
    c = _client()
    r = c.put("/api/tools/operations", json={"disabled_operations": {"alpha": ["read"]}}, headers=revision_headers(c, None, read_path='/api/tools'))
    assert r.status_code == 200
    assert r.json()["status"] == "partial"


def test_operations_unsupported_app_400(store_env):
    # #1531：子行程不認 disabled_operations 的 app 不能回「已套用」
    registry = _fake_registry()
    registry.OPERATION_POLICIES = {}
    c = _client(operations_supported=False, registry=registry)
    r = c.put("/api/tools/operations", json={"disabled_operations": {"alpha": ["read"]}}, headers=revision_headers(c, None, read_path='/api/tools'))
    assert r.status_code == 400


def test_registry_refresh_hook_called_before_list(store_env):
    reg = _fake_registry()
    calls = []

    async def refresh():
        calls.append(1)
        reg.TOOL_REGISTRY.append(_tool("gamma", _Cat.A, []))

    reg.refresh = refresh
    reg.get_all_tool_names = lambda: [t.name for t in reg.TOOL_REGISTRY]
    c = _client(registry=reg)
    body = c.get("/api/tools").json()
    assert calls == [1]
    assert "gamma" in {t["name"] for t in body["tools"]}


def test_registry_refresh_failure_keeps_previous_list(store_env):
    reg = _fake_registry()

    async def refresh():
        raise RuntimeError("upstream unavailable")

    reg.refresh = refresh
    body = _client(registry=reg).get("/api/tools").json()
    assert body["total"] == 2


def test_odoo_real_registry_serves_canon():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mcp_admin_core.routers.tools import create_tools_router
    from odoo_mcp_admin import tool_registry as odoo_reg

    app = FastAPI()
    app.include_router(create_tools_router(registry=odoo_reg, mcp_label="odoo-mcp"))
    body = TestClient(app).get("/api/tools").json()
    assert body["total"] == 41
    assert body["enabled"] == 41          # 全部 enabled_by_default → 預設全啟用
    assert len(body["categories"]) == 10  # odoo 十類
    operations = {t["name"]: t["operations"] for t in body["tools"] if t["operations"]}
    assert operations["execute_approved_write"] == ["create", "write", "unlink"]
    assert len(operations) == 4
    # 三支審批工具的 create／write／unlink 限制擋不住直接寫入的 execute_method：Permissions 頁要寫明（審查第三輪）
    notes = {t["name"]: t["operation_note"] for t in body["tools"] if t["operation_note"]}
    assert set(notes) == {"preview_write", "validate_write", "execute_approved_write"}
    for note in notes.values():
        assert "execute_method" in note and "Allow Direct Writes" in note and "Tools 頁停用 execute_method" in note


def test_k8s_real_registry_serves_canon():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mcp_admin_core.routers.tools import create_tools_router
    from k8s_mcp_admin import tool_registry as k8s_registry

    app = FastAPI()
    app.include_router(create_tools_router(registry=k8s_registry, mcp_label="k8s-mcp"))
    body = TestClient(app).get("/api/tools").json()
    assert body["total"] == 35            # 六個免設定工具集＋多叢集的 configuration_contexts_list（負責人 2026-10-25「預設全開」）
    assert body["enabled"] == 35          # 全部 enabled_by_default → 預設全啟用
    assert len(body["categories"]) == 7   # Core (Read) / Core (Write) / Config / Helm / kcp / KubeVirt / Tekton
    # operations 維度對 k8s 為空（工具無 per-op 概念）
    assert all(t["operations"] == [] for t in body["tools"])
    # dangerous 的 15 支 write tool 名稱需一致（UI 紅標語意）
    dangerous_names = {t["name"] for t in body["tools"] if t["dangerous"]}
    assert dangerous_names == {
        "pods_exec",
        "pods_run",
        "pods_delete",
        "resources_create_or_update",
        "resources_delete",
        "resources_scale",
        "helm_install",
        "helm_uninstall",
        "vm_clone",
        "vm_create",
        "vm_lifecycle",
        "tekton_pipeline_start",
        "tekton_pipelinerun_restart",
        "tekton_task_start",
        "tekton_taskrun_restart",
    }


def test_vk_real_registry_serves_canon():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mcp_admin_core.routers.tools import create_tools_router
    from vk_mcp_admin import tool_registry as vk_reg

    app = FastAPI()
    app.include_router(create_tools_router(registry=vk_reg, mcp_label="vibe-kanban-mcp"))
    body = TestClient(app).get("/api/tools").json()
    assert body["total"] == 34
    assert body["enabled"] == 34          # 全部 enabled_by_default → 預設全啟用
    assert len(body["categories"]) == 4   # A - Execution / B - Repo / C - Issue / Kanban / D - Context
    # vk 工具無 per-op 維度
    assert all(t["operations"] == [] for t in body["tools"])
    # 3 個 dangerous（UI 紅標；停用另由 core proxy 強制，見 README）
    dangerous_names = {t["name"] for t in body["tools"] if t["dangerous"]}
    assert dangerous_names == {"start_workspace", "delete_workspace", "delete_issue"}


def test_opendesign_real_registry_serves_canon():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mcp_admin_core.routers.tools import create_tools_router
    from opendesign_mcp_admin import tool_registry as opendesign_reg

    app = FastAPI()
    app.include_router(create_tools_router(registry=opendesign_reg, mcp_label="opendesign-mcp"))
    body = TestClient(app).get("/api/tools").json()
    assert body["total"] == 15
    assert body["enabled"] == 15          # 空 config → 預設全啟用
    assert len(body["categories"]) == 3   # System & Discovery / Projects & Files – Read / Projects – Write
    # opendesign 工具無 per-op 維度
    assert all(t["operations"] == [] for t in body["tools"])
    # dangerous 的 3 支 write/長時工具名稱需一致（UI 紅標，見 README）
    dangerous_names = {t["name"] for t in body["tools"] if t["dangerous"]}
    assert dangerous_names == {"create_project", "send_message", "delete_project"}
