"""條件寫入描述須反映儲存政策，不能把相依權限當成完整呼叫授權。"""
import pytest
from fastapi.testclient import TestClient

from mcp_admin_core.capabilities import tool_snapshot
from mcp_admin_core.app import create_app
from mcp_admin_core.operation_policy import disabled_operation_calls
from mcp_admin_core.routers.tools import create_tools_router
from n8n_mcp_admin import tool_registry

UPDATER = "n8n_update_partial_workflow"
EXPOSURES = {"n8n_workflow_versions", "n8n_test_workflow"}


@pytest.mark.parametrize("tools,allowed", [
    ({}, True),
    ({"disabled": [UPDATER]}, False),
    ({"disabled_operations": {UPDATER: ["retained"]}}, False),
    ({"disabled_operations": {UPDATER: []}}, True),
])
def test_條件描述沿用更新工具限制且不隱藏讀取(tools, allowed):
    """只描述相依政策，子行程尚未觀察時不得宣稱可執行。"""
    catalog = tool_snapshot(tool_registry, {"tools": tools}, None, None)
    for item in catalog["items"]:
        if item["name"] in EXPOSURES:
            assert item["conditional_writes"] == [{"parameter": "exposeToMcp",
                "requires_tool": UPDATER, "policy_enabled": allowed}]
            assert item["policy_enabled"] is True
            assert item["effective"] is None
        else:
            assert not item.get("conditional_writes")


async def test_儲存工具開關立即更新描述與呼叫限制(store_env, monkeypatch):
    """經真實管理 API 儲存並回讀；不啟動子行程或連線後端。"""
    async def no_refresh():
        """使用隨版本保存的工具登錄。"""
    monkeypatch.setattr(tool_registry, "refresh", no_refresh)
    retained = {"retired_tool": ["old"], "n8n_workflow_versions": ["rollback"]}
    await store_env.patch("tools", {"disabled_operations": retained, "custom_sibling": 42})
    app = create_app("條件寫入測試", product_id="n8n", tool_registry=tool_registry,
        extra_routers=[create_tools_router(registry=tool_registry, mcp_label="n8n")])
    client = TestClient(app)
    token = client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    for disabled, allowed in [([UPDATER], False), ([], True), ([UPDATER], False)]:
        before = client.get("/api/tools").json()
        response = client.put("/api/tools", json={"disabled_tools": disabled},
            headers={"If-Match": '"' + before["configuration"]["revision"] + '"'})
        assert response.status_code == 200
        assert response.json()["apply_state"] == "proxy_policy_applied"
        saved = await store_env.get("tools")
        assert saved["disabled_operations"] == retained and saved["custom_sibling"] == 42
        for item in client.get("/api/tools").json()["tools"]:
            if item["name"] in EXPOSURES:
                assert item["conditional_writes"][0]["policy_enabled"] is allowed
                for flag in (True, False):
                    call = {"method": "tools/call", "params": {"name": item["name"],
                        "arguments": {"mode": "list", "method": "prepare", "exposeToMcp": flag}}}
                    assert bool(disabled_operation_calls(call, saved, tool_registry)) is (flag and not allowed)
