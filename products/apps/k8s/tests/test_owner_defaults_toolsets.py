"""k8s：既有設定第一次以新映像啟動、一次性改成全開時，toolsets 取舊值與全開值的聯集（第二輪驗證 F5）。

fc5fa02 的 owner_defaults 以 FRESH_ENV 整個覆寫 K8S_MCP_TOOLSETS：10-25 之前自己開了 kiali、netobserv 的服務，
第一次用新映像啟動時這些工具集就不見了。現在 LaunchPolicy.fresh_env_union_keys 宣告 K8S_MCP_TOOLSETS：
以全開值的順序為準、再照原順序補上舊值多出來的項目（kiali、netobserv…），去重；舊值已涵蓋全開項目就不動；
其他治理開關照舊改成全開。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mcp_admin_core.config.store import ConfigStore
from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy
from mcp_admin_core.owner_defaults import MARKER, apply_owner_defaults

from k8s_mcp_admin.launcher import FRESH_ENV, FRESH_TOOLSETS
from k8s_mcp_admin.main import LAUNCH_POLICY


@pytest.fixture
def k8s_policy():
    saved = get_launch_policy()
    set_launch_policy(LAUNCH_POLICY)
    yield LAUNCH_POLICY
    set_launch_policy(saved)


def _migrate(config_path, env: dict) -> dict:
    config_path.write_text(json.dumps({
        "connection": {"in_cluster": "1"},
        "tools": {"disabled": ["pods_delete"], "disabled_operations": {}},
        "mcp_server": {"command": "k8s-mcp-http", "port": 8000, "env_prefix": "K8S_MCP_", "env": env},
    }))
    asyncio.run(apply_owner_defaults(ConfigStore(path=config_path)))
    return json.loads(config_path.read_text())


def test_kiali_and_netobserv_survive_the_migration(config_path, k8s_policy):
    legacy = {"K8S_MCP_READ_ONLY": "1", "K8S_MCP_DISABLE_DESTRUCTIVE": "1", "K8S_MCP_ALLOW_SECRETS": "0",
              "K8S_MCP_TOOLSETS": "core,config,helm,kiali,netobserv"}
    cfg = _migrate(config_path, legacy)
    env = cfg["mcp_server"]["env"]
    assert env["K8S_MCP_TOOLSETS"] == "config,core,helm,kcp,kubevirt,tekton,kiali,netobserv"
    assert set(FRESH_TOOLSETS) <= set(env["K8S_MCP_TOOLSETS"].split(","))
    for key in ("K8S_MCP_READ_ONLY", "K8S_MCP_DISABLE_DESTRUCTIVE", "K8S_MCP_ALLOW_SECRETS", "K8S_MCP_MULTI_CLUSTER"):
        assert env[key] == FRESH_ENV[key], key
    assert cfg["tools"]["disabled"] == [] and cfg["tools"][MARKER]["applied"] == "migrated"


def test_legacy_default_toolsets_become_the_fresh_value(config_path, k8s_policy):
    """改版前的出廠值 core,config,helm（或缺鍵）：正好變成新安裝（與 PaaS 範本 seed）的全開值。"""
    for toolsets in ("core,config,helm", None):
        legacy = {"K8S_MCP_READ_ONLY": "1"} if toolsets is None else {"K8S_MCP_TOOLSETS": toolsets}
        env = _migrate(config_path, legacy)["mcp_server"]["env"]
        assert env["K8S_MCP_TOOLSETS"] == FRESH_ENV["K8S_MCP_TOOLSETS"] == ",".join(FRESH_TOOLSETS)


def test_owner_order_is_kept_when_nothing_is_missing(config_path, k8s_policy):
    toolsets = "tekton,kubevirt,kcp,helm,core,config,kiali"
    env = _migrate(config_path, {"K8S_MCP_TOOLSETS": toolsets, "K8S_MCP_READ_ONLY": "1"})["mcp_server"]["env"]
    assert env["K8S_MCP_TOOLSETS"] == toolsets


def test_policy_declares_toolsets_as_a_union_key():
    assert LAUNCH_POLICY.fresh_env_union_keys == frozenset({"K8S_MCP_TOOLSETS"})
