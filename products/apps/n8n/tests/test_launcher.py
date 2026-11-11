"""n8n MCP HTTP launcher 的契約測試。

對外契約：MCP client 經 admin reverse proxy 打 ``/private_{token}/mcp``，proxy 反代到
``127.0.0.1:{mcp_server.port}/mcp``（Streamable HTTP，預設 port 3000）。

woow_paas_platform#1531：launcher 改跑上游建議的 ``dist/mcp/index.js``（``MCP_MODE=http``、不設
``USE_FIXED_HTTP`` → SingleSessionHTTPServer），不再跑 deprecated 的 ``dist/http-server.js``。
套件位置以 ``npm root -g`` 動態定位。其餘契約（Permissions、Session、工具清單）見
packages/mcp-admin-core/tests/test_n8n_app.py。
"""
from __future__ import annotations

import pytest


def test_launcher_main_is_importable_and_callable() -> None:
    from n8n_mcp_admin.launcher import main

    assert callable(main)


def test_package_dir_uses_npm_root_g(monkeypatch) -> None:
    from n8n_mcp_admin import upstream

    calls: dict = {}

    class _Result:
        stdout = "/usr/lib/node_modules\n"

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return _Result()

    monkeypatch.delenv("N8N_MCP_PACKAGE_DIR", raising=False)
    monkeypatch.setattr(upstream.subprocess, "run", fake_run)
    upstream.package_dir.cache_clear()
    try:
        assert upstream.entrypoint() == "/usr/lib/node_modules/n8n-mcp/dist/mcp/index.js"
        assert calls["cmd"] == ["npm", "root", "-g"]
    finally:
        upstream.package_dir.cache_clear()


def test_main_execs_node_mcp_index_with_http_env(monkeypatch) -> None:
    from n8n_mcp_admin import launcher

    monkeypatch.setattr(launcher, "resolve_entrypoint", lambda: "/x/n8n-mcp/dist/mcp/index.js")
    # 本測試只驗 exec 契約；真 DB 初始化與持久性由 core 的整合測試驗證。
    monkeypatch.setattr(launcher, "prepare_database", lambda *args: "/data/n8n-mcp/nodes.db")
    monkeypatch.setenv("USE_FIXED_HTTP", "true")
    captured: dict = {}

    def fake_execvpe(file, argv, env):
        captured.update(file=file, argv=argv, env=env)
        raise SystemExit(0)  # exec 不返回；以 SystemExit 模擬替換行程

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    with pytest.raises(SystemExit):
        launcher.main([])

    assert captured["file"] == "node"
    assert captured["argv"] == ["node", "/x/n8n-mcp/dist/mcp/index.js"]
    assert captured["env"]["MCP_MODE"] == "http"
    assert captured["env"]["PORT"] == "3000"
    assert captured["env"]["HOST"] == "127.0.0.1"
    assert captured["env"]["NODE_DB_PATH"] == "/data/n8n-mcp/nodes.db"
    assert "USE_FIXED_HTTP" not in captured["env"]
