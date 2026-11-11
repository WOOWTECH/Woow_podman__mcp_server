"""端到端：core McpProcessManager 真的 spawn ``nextcloud-mcp-server``，子行程以 Streamable HTTP 在 127.0.0.1 提供
政策允許的工具。不連 Nextcloud（列工具不需要後端；base_url 指向不存在的網域）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import socket

import httpx
import pytest
from fastmcp import Client

from nextcloud_mcp_admin import tool_registry

from .fake_nextcloud import LOGIN, PASSWORD

pytestmark = pytest.mark.skipif(shutil.which("nextcloud-mcp-server") is None,
                                reason="console script not installed (uv sync)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_tools(url: str, timeout: float = 30.0) -> list[str]:
    deadline = asyncio.get_running_loop().time() + timeout
    last: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with Client(url) as client:
                return [t.name for t in await client.list_tools()]
        except Exception as exc:  # noqa: BLE001 — 子行程還沒起來
            last = exc
            await asyncio.sleep(0.3)
    raise AssertionError(f"MCP child did not answer on {url}: {last!r}")


@pytest.mark.parametrize(
    ("disabled", "expected"),
    [
        (tool_registry.default_disabled_tools(), tool_registry.get_all_tool_names()),  # 新安裝：9 支全開
        (tool_registry.write_and_dangerous_tool_names(), tool_registry.read_only_tool_names()),
        (["delete_file_checked"], [n for n in tool_registry.get_all_tool_names() if n != "delete_file_checked"]),
    ],
)
async def test_process_manager_spawns_child_with_policy(tmp_path, monkeypatch, caplog, disabled, expected):
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod

    caplog.set_level(logging.INFO, logger="mcp_admin_core.process")
    port = _free_port()
    cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    cfg["connection"] = {"base_url": "https://nextcloud.invalid", "username": LOGIN, "app_password": PASSWORD}
    cfg["tools"] = {"disabled": disabled, "nextcloud_policy_seeded": True}
    cfg["mcp_server"] = {"command": "nextcloud-mcp-server", "args": ["--port", str(port)], "port": port,
                         "env": {"NEXTCLOUD_MCP_READONLY": "false", "NEXTCLOUD_MCP_ALLOW_DELETE": "true"},
                         "env_prefix": "NEXTCLOUD_MCP_"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
    monkeypatch.setattr(store_mod, "_instance", None)
    monkeypatch.setattr(process_mod, "_instance", None)

    pm = process_mod.get_process_manager()
    assert await pm.start() is True
    try:
        names = await _wait_tools(f"http://127.0.0.1:{port}/mcp")
        # mcp_server.env 手動填的 READONLY／ALLOW_DELETE 被 launcher 依政策覆寫
        assert names == expected
        status = await pm.status()
        assert status["running"] is True
        assert PASSWORD not in " ".join([status["command"], *cfg["mcp_server"]["args"]])
    finally:
        await pm.stop()
    # Readiness probes add protocol logs. The bounded UI tail can evict the
    # startup policy line; assert against the complete captured child output.
    output = "\n".join(record.getMessage() for record in caplog.records
                       if record.name == "mcp_admin_core.process")
    assert PASSWORD not in output
    assert "tool policy" in output
    # FASTMCP_SHOW_SERVER_BANNER／FASTMCP_CHECK_FOR_UPDATES 生效：沒有 banner 框，也沒有 PyPI 更新提示
    assert "╭" not in output and "Update" not in output


INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "ci", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def test_core_proxy_reaches_child_despite_host_and_origin_checks(tmp_path, monkeypatch):
    """vendored server（v0.1.0 起）會對陌生 Host 回 421、陌生 Origin 回 403；core proxy 改寫 Host、丟掉 Origin，
    所以經 /private_<token>/mcp 帶公開網域與瀏覽器 Origin 仍可用，直連子行程帶公開 Host 則被擋。"""
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod
    import mcp_admin_core.proxy as proxy_mod

    port = _free_port()
    cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    cfg["mcp_auth_token"] = "ci-token-0123456789"
    cfg["connection"] = {"base_url": "https://nextcloud.invalid", "username": LOGIN, "app_password": PASSWORD}
    cfg["tools"] = {"disabled": tool_registry.write_and_dangerous_tool_names(), "nextcloud_policy_seeded": True}
    cfg["mcp_server"] = {"command": "nextcloud-mcp-server", "args": ["--port", str(port)], "port": port,
                         "env": {}, "env_prefix": "NEXTCLOUD_MCP_"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
    monkeypatch.setattr(store_mod, "_instance", None)
    monkeypatch.setattr(process_mod, "_instance", None)
    monkeypatch.setattr(proxy_mod, "_client", None)

    from nextcloud_mcp_admin.main import app

    pm = process_mod.get_process_manager()
    assert await pm.start() is True
    try:
        await _wait_tools(f"http://127.0.0.1:{port}/mcp")
        async with httpx.AsyncClient() as direct:
            blocked = await direct.post(f"http://127.0.0.1:{port}/mcp", json=INITIALIZE,
                                        headers={**MCP_HEADERS, "Host": "mcp.example.com"})
        assert blocked.status_code == 421

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://mcp.example.com") as client:
            resp = await client.post("/private_ci-token-0123456789/mcp", json=INITIALIZE,
                                     headers={**MCP_HEADERS, "Origin": "https://claude.ai"})
            assert resp.status_code == 200, resp.text
            assert "Woow Nextcloud" in resp.text
            denied = await client.post("/private_wrong-token/mcp", json=INITIALIZE, headers=MCP_HEADERS)
            assert denied.status_code == 403
    finally:
        await pm.stop()
        if proxy_mod._client is not None:
            await proxy_mod._client.aclose()


# ---------------------------------------------------------------------------
# 大檔上傳端到端：MCP Python SDK client → admin 閘道（真的 uvicorn）→ 子行程 → 假 Nextcloud（真的 HTTP）
# ---------------------------------------------------------------------------

MiB = 1024 * 1024


class _FakeNextcloudHTTP:
    """把 ``FakeNextcloud``（httpx.MockTransport 用的替身）掛成真的 HTTP server，讓另一個行程的子行程連得到。"""

    def __init__(self, fake) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                request = httpx.Request(self.command, "http://cloud.example.com" + self.path,
                                        headers=list(self.headers.items()),
                                        content=self.rfile.read(length) if length else b"")
                response = fake.handle(request)
                data = response.read()
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() not in ("content-length", "transfer-encoding", "connection"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_PUT = do_PROPFIND = do_DELETE = do_REPORT = _any

            def log_message(self, *args) -> None:  # noqa: D401 — 測試不印存取 log
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/nc"  # FakeNextcloud 的路徑前綴是 /nc

    def __enter__(self):
        import threading

        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def _serve_admin(app):
    """以真的 uvicorn（含 lifespan：seed 預設值＋拉起子行程）在背景執行緒跑 admin；回傳 (port, stop)。"""
    import threading
    import time

    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "admin did not start"
        time.sleep(0.05)

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=30)

    return port, stop


def test_large_uploads_pass_gateway_and_child_up_to_the_product_limit(tmp_path, monkeypatch):
    """mcp 1.30.0 的 transport 預設 4 MiB：修正前 base64 超過 4 MiB（約 3 MiB 的檔案）的上傳在子行程就被 413。

    現在 16 MiB 的上限（mcp_admin_core/request_body.py）涵蓋產品上限 10 MiB 的檔案（約 13.3 MiB 的請求）；
    再大的檔案由子行程自己回可讀的工具錯誤，超過 16 MiB 的請求由閘道回帶 id 的 JSON-RPC 錯誤（不是看不懂的 413）。
    """
    import base64
    import os

    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod
    import mcp_admin_core.proxy as proxy_mod
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import MCPError
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

    from nextcloud_mcp_admin import main as admin_main

    from .fake_nextcloud import FakeNextcloud

    fake = FakeNextcloud()
    token = "ci-token-0123456789"
    child_port = _free_port()
    with _FakeNextcloudHTTP(fake) as backend:
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        cfg["mcp_auth_token"] = token
        cfg["connection"] = {"base_url": backend.base_url, "username": LOGIN, "app_password": PASSWORD}
        cfg["tools"] = {"disabled": ["delete_file_checked"], "nextcloud_policy_seeded": True}  # 開放 upload_file
        cfg["mcp_server"] = {"command": "nextcloud-mcp-server", "args": ["--port", str(child_port)],
                             "port": child_port, "env": {}, "env_prefix": "NEXTCLOUD_MCP_"}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(cfg))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
        monkeypatch.setattr(store_mod, "_instance", None)
        monkeypatch.setattr(process_mod, "_instance", None)
        monkeypatch.setattr(proxy_mod, "_client", None)
        previous_policy = get_launch_policy()
        set_launch_policy(admin_main.LAUNCH_POLICY)  # 正式環境的 command／args 與 env 白名單
        port, stop = _serve_admin(admin_main.app)

        def upload(name: str, data: bytes) -> dict:
            return {"path": name, "content_base64": base64.b64encode(data).decode()}

        async def scenario() -> None:
            url = f"http://127.0.0.1:{port}/private_{token}/mcp"
            await _wait_tools(url, timeout=60)
            async with streamable_http_client(url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # 4 MiB 以上、產品上限以內：經閘道與子行程寫進 Nextcloud，內容一個 byte 都不差
                    for name, size in (("six.bin", 6 * MiB), ("ten.bin", 10 * MiB)):
                        data = os.urandom(size)
                        assert 4 * MiB < len(json.dumps(upload(name, data))) < 16 * MiB
                        result = await session.call_tool("upload_file", upload(name, data))
                        assert not result.is_error, result.content
                        assert result.structured_content["status"] == "created"
                        assert result.structured_content["bytes"] == size
                        assert fake.files[name].data == data
                    # 產品上限多 1 byte：請求本身仍在 16 MiB 內，由子行程回它自己的可讀錯誤
                    over = await session.call_tool("upload_file", upload("over.bin", b"\0" * (10 * MiB + 1)))
                    assert over.is_error and "upload limit" in over.content[0].text
                    # 請求超過 16 MiB：閘道不轉發，回帶 id 的 JSON-RPC 錯誤——client 只有這次呼叫失敗
                    with pytest.raises(MCPError) as refused:
                        await session.call_tool("upload_file", upload("huge.bin", b"\0" * (13 * MiB)))
                    assert "16 MiB" in str(refused.value) and "上限" in str(refused.value)
                    assert "huge.bin" not in fake.files and "over.bin" not in fake.files
                    tree = await session.call_tool("get_file_tree", {"path": ""})  # 同一個 session 照常可用
                    assert not tree.is_error and "ten.bin" in json.dumps(tree.structured_content)

        try:
            asyncio.run(asyncio.wait_for(scenario(), timeout=240))
        finally:
            stop()
            set_launch_policy(previous_policy)
