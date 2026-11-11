"""connection 鍵＋env_prefix 必須對上子行程 Settings 的 env 名（改寫自來源 repo 同名測試）。

core ``_build_subprocess_env`` 會把 ``connection`` 各鍵大寫並加 ``mcp_server.env_prefix``，再疊上
``mcp_server.env``。PaaS seed：``env_prefix="LITELLM_MCP_"``、``env.LITELLM_MCP_READONLY="0"``（新安裝全開，
負責人 2026-10-25「預設全開」；之前的 seed 是 ``"1"``）、平台預填 ``connection.base_url``，金鑰由租戶在 Connection
頁貼 ``master_key``。
"""
from __future__ import annotations

from mcp_admin_core.process import _build_subprocess_env

from litellm_mcp_admin import seed_defaults
from litellm_mcp_admin.routers.config import CONNECTION_KEYS
from woow_litellm_mcp_server.gating import ToolGate
from woow_litellm_mcp_server.settings import Settings

PAAS_SEED_MCP_SERVER = {
    "command": "litellm-mcp-server",
    "args": ["--port", "8000"],
    "port": 8000,
    "env_prefix": "LITELLM_MCP_",
    "env": {"LITELLM_MCP_READONLY": "0"},
}


def test_connection_keys_map_onto_settings_fields():
    assert CONNECTION_KEYS == ("base_url", "master_key")
    assert seed_defaults.ENV_PREFIX == "LITELLM_MCP_"
    for key in CONNECTION_KEYS:
        assert key in Settings.model_fields, key


def test_paas_seed_env_loads_into_settings(monkeypatch):
    conn = {"base_url": "http://svc-abcd1234-litellm:4000", "master_key": "sk-viewer-123456"}
    env = _build_subprocess_env(conn, PAAS_SEED_MCP_SERVER)
    assert env["LITELLM_MCP_BASE_URL"] == "http://svc-abcd1234-litellm:4000"
    assert env["LITELLM_MCP_MASTER_KEY"] == "sk-viewer-123456"
    assert env["LITELLM_MCP_READONLY"] == "0"

    for k in ("LITELLM_MCP_BASE_URL", "LITELLM_MCP_MASTER_KEY", "LITELLM_MCP_READONLY"):
        monkeypatch.setenv(k, env[k])
    cfg = Settings(_env_file=None)
    assert cfg.base_url == "http://svc-abcd1234-litellm:4000"
    assert cfg.master_key == "sk-viewer-123456"
    assert cfg.readonly is False
    assert len(ToolGate(cfg).enabled_tool_names()) == 40  # 新安裝：40 支全開


def test_fresh_readonly_value_is_all_on():
    assert seed_defaults.FRESH_READONLY == "0"


def test_readonly_seed_exposes_exactly_15_read_tools(monkeypatch):
    """Settings 頁打開唯讀（或 2026-10-25 之前開通、seed 寫 "1" 的服務）：只剩 15 支查詢工具。"""
    monkeypatch.setenv("LITELLM_MCP_READONLY", "1")
    gate = ToolGate(Settings(_env_file=None))
    names = gate.enabled_tool_names()
    assert len(names) == 15
    assert "litellm_health_readiness" in names and "litellm_list_keys" in names
    assert "litellm_generate_key" not in names and "litellm_delete_key" not in names
    assert "litellm_chat_completion" not in names  # 會花錢的 create 也在唯讀時隱藏


def test_settings_toggle_values_parse(monkeypatch):
    # Settings 頁開關寫 '1'／'0'
    monkeypatch.setenv("LITELLM_MCP_READONLY", "0")
    assert Settings(_env_file=None).readonly is False
    gate = ToolGate(Settings(_env_file=None))
    assert len(gate.enabled_tool_names()) == 40


def test_empty_prefix_would_break_wiring():
    """seed_defaults 補 env_prefix 的理由：前綴空著，子行程拿不到網址與金鑰。"""
    env = _build_subprocess_env({"base_url": "http://x:4000", "master_key": "k"}, {"env_prefix": ""})
    assert "LITELLM_MCP_BASE_URL" not in env and env["BASE_URL"] == "http://x:4000"
