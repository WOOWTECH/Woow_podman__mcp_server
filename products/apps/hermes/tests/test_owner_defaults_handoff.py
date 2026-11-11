"""Record the intentional owner-default transition through the real Hermes app lifespan."""

import json

import mcp_admin_core.config.store as store_mod
import mcp_admin_core.process as process_mod
import pytest
from fastapi.testclient import TestClient
from hermes_mcp_admin.main import LAUNCH_POLICY, app
from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy
from mcp_admin_core.owner_defaults import MARKER


@pytest.mark.parametrize("already_marked", [False, True], ids=["legacy_unmarked", "already_migrated"])
def test_hermes_lifespan_applies_owner_decision_before_child_start(tmp_path, monkeypatch, already_marked):
    config_path = tmp_path / "config.json"
    config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    config["mcp_auth_token"] = "synthetic-token"
    config["token_history"] = [{"action": "set", "rotated_at": "synthetic-time"}]
    config["connection"] = {"dashboard_url": "http://127.0.0.1:1", "dashboard_password": "synthetic-password"}
    config["tools"]["disabled"] = ["hermes_gateway"]
    config["tools"]["disabled_operations"] = {"hermes_gateway": ["restart"]}
    if already_marked:
        config["tools"][MARKER] = {"applied": "migrated", "at": "synthetic-time"}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    observed = {}

    class _ObservedProcessManager:
        async def start(self):
            observed.update(await store_mod.get_config_store().load())
            return True

        async def stop(self):
            return None

    monkeypatch.setattr(process_mod, "_instance", _ObservedProcessManager())
    previous_policy = get_launch_policy()
    try:
        set_launch_policy(LAUNCH_POLICY)
        with TestClient(app):
            pass
    finally:
        set_launch_policy(previous_policy)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert observed == saved  # child start sees the post-lifespan state
    assert saved["connection"] == config["connection"]
    assert saved["mcp_auth_token"] == config["mcp_auth_token"]
    assert saved["token_history"] == config["token_history"]
    assert saved["mcp_server"]["env"] == config["mcp_server"]["env"]
    if already_marked:
        assert saved == config  # current marker prevents a repeat migration
    else:
        assert saved["tools"]["disabled"] == []
        assert saved["tools"]["disabled_operations"] == {}
        assert saved["tools"][MARKER]["applied"] == "migrated"
        assert saved["tools"][MARKER]["source"] == "existing_config"
