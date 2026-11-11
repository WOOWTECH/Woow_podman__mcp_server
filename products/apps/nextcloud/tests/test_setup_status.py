"""新開的 Nextcloud MCP 在存好連線之前：MCP 用戶端拿到可讀的設定步驟，存好之後銜接到真的子行程。

2026-10-27 之前：子行程缺 NEXTCLOUD_MCP_BASE_URL／USERNAME／APP_PASSWORD 就以 exit 2 結束，閘道對 initialize、tools/list
一律回 HTTP 503，平台卻顯示「運作中」（round3 staging）。現在閘道回一個合法的最小 MCP 伺服器（mcp_admin_core/setup_status.py），
說明要到 Connection 頁填哪三個欄位；存好之後佔位 session 回 404，重新 initialize 就是真的 9 支工具。

端到端：真的 uvicorn（含 lifespan，真的 spawn ``nextcloud-mcp-server``）、真的 MCP Python SDK client 與 fastmcp Client、
假 Nextcloud 掛成真的 HTTP server；連線經管理 API（PUT /api/config/connection）存檔。
"""
from __future__ import annotations

from management_client import async_revision_headers
import asyncio
import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

from nextcloud_mcp_admin import tool_registry
from nextcloud_mcp_admin.main import LAUNCH_POLICY

from .fake_nextcloud import LOGIN, PASSWORD
# 與子行程端到端測試共用：真的 uvicorn admin、掛成 HTTP 的假 Nextcloud、等子行程列出工具
from .test_child_process import INITIALIZE, MCP_HEADERS, _FakeNextcloudHTTP, _free_port, _serve_admin, _wait_tools

FRONTEND_CONFIG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "appConfig.jsx"
ADMIN_PASSWORD = "admin-pw-for-setup-status-test"


def test_required_fields_are_the_three_connection_fields_with_their_page_labels():
    labels = dict(re.findall(r"\{ name: '(\w+)', label: '([^']+)'", FRONTEND_CONFIG.read_text(encoding="utf-8")))
    assert LAUNCH_POLICY.required_connection == tuple(
        (key, labels[key]) for key in ("base_url", "username", "app_password"))


@pytest.mark.skipif(shutil.which("nextcloud-mcp-server") is None, reason="console script not installed (uv sync)")
def test_fresh_service_explains_the_setup_then_hands_over_once_the_connection_is_saved(tmp_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod
    import mcp_admin_core.proxy as proxy_mod
    from fastmcp import Client
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp_admin_core import setup_status
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

    from nextcloud_mcp_admin import main as admin_main

    from .fake_nextcloud import FakeNextcloud

    fake = FakeNextcloud()
    fake.add_folder("Docs")
    token = "ci-token-setup-status-0123456789"
    child_port = _free_port()
    with _FakeNextcloudHTTP(fake) as backend:
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        cfg["mcp_auth_token"] = token
        cfg["admin_password"] = ADMIN_PASSWORD
        cfg["connection"] = {}  # PaaS 新服務：平台只 seed 管理密碼
        cfg["mcp_server"] = {"command": "nextcloud-mcp-server", "args": ["--port", str(child_port)],
                             "port": child_port, "env": {}, "env_prefix": "NEXTCLOUD_MCP_"}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(cfg))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
        monkeypatch.setattr(store_mod, "_instance", None)
        monkeypatch.setattr(process_mod, "_instance", None)
        monkeypatch.setattr(proxy_mod, "_client", None)
        previous_policy = get_launch_policy()
        set_launch_policy(admin_main.LAUNCH_POLICY)
        port, stop = _serve_admin(admin_main.app)  # lifespan 拉起子行程：缺連線 → exit 2，看門狗之後再重試
        base = f"http://127.0.0.1:{port}"
        url = f"{base}/private_{token}/mcp"

        async def scenario() -> None:
            async with httpx.AsyncClient(base_url=base, timeout=30) as http:
                assert (await http.get("/healthz")).json() == {"status": "ok", "mcp": "not_configured"}

                # 真的 MCP Python SDK client：握手成功，instructions／工具／工具結果都說要到 Connection 頁填哪三個欄位
                async with streamable_http_client(url) as (read, write):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        assert "Nextcloud" in init.server_info.name and "尚未設定" in init.server_info.name
                        assert f"{base}/config" in init.instructions
                        for key, label in LAUNCH_POLICY.required_connection:
                            assert f"{label}（{key}）" in init.instructions
                        listed = await session.list_tools()
                        assert [tool.name for tool in listed.tools] == [setup_status.SETUP_TOOL]
                        result = await session.call_tool(setup_status.SETUP_TOOL, {})
                        assert result.is_error and result.content[0].text == init.instructions
                        for secret in (token, ADMIN_PASSWORD):
                            assert secret not in init.instructions
                # 另一個用戶端實作（fastmcp）也接受這個佔位伺服器
                async with Client(url) as other:
                    assert [tool.name for tool in await other.list_tools()] == [setup_status.SETUP_TOOL]

                held = await http.post(f"/private_{token}/mcp", json=INITIALIZE, headers=MCP_HEADERS)
                placeholder = held.headers["mcp-session-id"]
                assert placeholder.startswith(setup_status.PLACEHOLDER_SESSION_PREFIX)
                # legacy SSE 照舊：子行程沒在跑 503；它正在啟動或被看門狗重啟（缺連線，約 1–2 秒後又 exit 2）時 502
                assert (await http.get(f"/private_{token}/sse")).status_code in (502, 503)

                # 管理員在 Connection 頁存好連線（restart：子行程沒在跑就 start）
                login = await http.post("/api/auth/login", json={"password": ADMIN_PASSWORD})
                saved = await http.put("/api/config/connection", headers=await async_revision_headers(http, {"Authorization": f"Bearer {login.json()['token']}"}, read_path="/api/config"),
                                       json={"base_url": backend.base_url, "username": LOGIN,
                                             "app_password": PASSWORD, "restart": True})
                assert saved.status_code == 200, saved.text
                assert (await http.get("/healthz")).json() == {"status": "ok", "mcp": "running"}
                assert sorted(await _wait_tools(url, timeout=60)) == sorted(tool_registry.get_all_tool_names())

                # 還帶著佔位 session 的用戶端：404（與 MCP SDK 對不存在的 session 回的同形狀）→ 依規格重新 initialize
                gone = await http.post(f"/private_{token}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                       headers={**MCP_HEADERS, "Mcp-Session-Id": placeholder})
                assert gone.status_code == 404
                assert gone.json() == {"jsonrpc": "2.0", "id": None,
                                       "error": {"code": -32600, "message": "Session not found"}}
                async with streamable_http_client(url) as (read, write):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        assert init.server_info.name == "Woow Nextcloud"
                        tree = await session.call_tool("get_file_tree", {"path": ""})
                        assert not tree.is_error and "Docs" in json.dumps(tree.structured_content)

        try:
            asyncio.run(asyncio.wait_for(scenario(), timeout=180))
        finally:
            stop()
            set_launch_policy(previous_policy)
