"""woow_paas_platform#1615：n8n-mcp 的匿名使用統計（telemetry）與 npm 版本檢查在 PaaS 一律關閉、租戶打不開。

- launcher 寫死上游開關 ``N8N_MCP_TELEMETRY_DISABLED=true``，蓋掉繼承來的 "false"，並移除改收集端的變數；
- LaunchPolicy 的 env 白名單不讓 config.json／管理頁帶進任何 telemetry 變數；
- launcher 同樣在最後寫死 ``N8N_MCP_VERSION_CHECK_DISABLED=true``（n8n-mcp 2.96+ 的 npm 版本檢查開關）；
- 有上游套件時（映像內，或本機以 ``N8N_MCP_PACKAGE_DIR`` 指到解開的 npm tarball）實際載入上游的
  config manager 與 npm version checker，確認 ``isEnabled()`` 為 false、不印 first-run 說明框、版本檢查回
  ``disabled`` 且沒有發出請求（映像建置時 Dockerfile 也會跑一次）。
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

TELEMETRY_VARS = ("N8N_MCP_TELEMETRY_DISABLED", "TELEMETRY_DISABLED", "DISABLE_TELEMETRY")
VERSION_CHECK_VAR = "N8N_MCP_VERSION_CHECK_DISABLED"


def test_build_env_forces_telemetry_off_over_inherited_values() -> None:
    from n8n_mcp_admin import launcher

    base = {
        "N8N_MCP_TELEMETRY_DISABLED": "false",
        "N8N_MCP_TELEMETRY_URL": "https://collector.example.com",
        "N8N_MCP_TELEMETRY_KEY": "k",
    }
    env = launcher.build_env("127.0.0.1", 3000, base)
    assert env["N8N_MCP_TELEMETRY_DISABLED"] == "true"
    assert "N8N_MCP_TELEMETRY_URL" not in env
    assert "N8N_MCP_TELEMETRY_KEY" not in env


def test_build_env_sets_telemetry_off_with_empty_base() -> None:
    from n8n_mcp_admin import launcher

    assert launcher.build_env("127.0.0.1", 3000, {})["N8N_MCP_TELEMETRY_DISABLED"] == "true"


@pytest.mark.parametrize("inherited", [None, "false", "0", "", "no", "true"])
def test_build_env_forces_npm_version_check_off(inherited) -> None:
    """上游只認 "true"／"1"；不論繼承值為何，子行程拿到的都必須是 "true"。"""
    from n8n_mcp_admin import launcher

    base = {} if inherited is None else {VERSION_CHECK_VAR: inherited}
    assert launcher.build_env("127.0.0.1", 3000, base)[VERSION_CHECK_VAR] == "true"


def test_launcher_main_execs_with_telemetry_off(monkeypatch) -> None:
    from n8n_mcp_admin import launcher

    monkeypatch.setattr(launcher, "resolve_entrypoint", lambda: "/x/n8n-mcp/dist/mcp/index.js")
    monkeypatch.setattr(launcher, "prepare_database", lambda *args: "/data/n8n-mcp/nodes.db")
    # main() 先呼叫 upstream.package_dir()（未設 N8N_MCP_PACKAGE_DIR 時跑 `npm root -g`）；不依賴 PATH 上有 npm。
    monkeypatch.setattr(launcher.upstream, "package_dir", lambda: "/x/n8n-mcp")
    monkeypatch.setenv("N8N_MCP_TELEMETRY_DISABLED", "0")
    monkeypatch.setenv(VERSION_CHECK_VAR, "0")
    captured: dict = {}

    def fake_execvpe(file, argv, env):
        captured.update(env=env)
        raise SystemExit(0)

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    with pytest.raises(SystemExit):
        launcher.main([])
    assert captured["env"]["N8N_MCP_TELEMETRY_DISABLED"] == "true"
    assert captured["env"][VERSION_CHECK_VAR] == "true"


def test_tenant_config_cannot_inject_telemetry_env() -> None:
    """管理頁／config.json 想從 mcp_server.env 或 connection 打開 telemetry → 白名單擋掉。"""
    from mcp_admin_core.process import _build_subprocess_env

    from n8n_mcp_admin.main import LAUNCH_POLICY

    assert not {*TELEMETRY_VARS, VERSION_CHECK_VAR} & set(LAUNCH_POLICY.env_keys)
    assert not {*TELEMETRY_VARS, VERSION_CHECK_VAR} & set(LAUNCH_POLICY.editable_env_keys)
    cfg = {
        "connection": {"n8n_api_url": "https://n8n.example.com", "n8n_api_key": "k",
                       "n8n_mcp_telemetry_disabled": "false", "telemetry_disabled": "false"},
        "mcp_server": {"env": {"AUTH_TOKEN": "t", "N8N_MCP_TELEMETRY_DISABLED": "false",
                               "N8N_MCP_TELEMETRY_URL": "https://collector.example.com",
                               VERSION_CHECK_VAR: "false"}},
    }
    env = _build_subprocess_env(cfg["connection"], cfg["mcp_server"], LAUNCH_POLICY, cfg)
    for name in (*TELEMETRY_VARS, "N8N_MCP_TELEMETRY_URL", VERSION_CHECK_VAR):
        assert os.environ.get(name) == env.get(name)  # 只可能是父行程原本的值，config 帶不進來


def _package_dir() -> str | None:
    override = os.environ.get("N8N_MCP_PACKAGE_DIR")
    if override:
        return override if os.path.isdir(override) else None
    if not shutil.which("npm"):
        return None
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    path = os.path.join(root.stdout.strip(), "n8n-mcp")
    return path if os.path.isdir(path) else None


def test_upstream_config_manager_reports_disabled() -> None:
    pkg = _package_dir()
    if not pkg or not shutil.which("node"):
        pytest.skip("n8n-mcp 套件不在本機（映像建置時 Dockerfile 會跑 telemetry_check）")
    from n8n_mcp_admin import telemetry_check

    assert telemetry_check.probe(pkg) == {"disabledByEnv": True, "enabled": False, "banner": False,
                                          "versionCheck": "disabled", "versionCheckRequests": 0}
    assert telemetry_check.main([pkg]) == 0
