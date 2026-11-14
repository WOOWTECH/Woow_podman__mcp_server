"""網址結尾多一個斜線（``/private_<token>/mcp/``）：當成 ``/mcp`` 處理；子行程的轉址不把內部位址回給用戶端（P3-c）。

證據（mp-odoo-iso-20261113 receipts/s04b-trailing-slash-redirect.json）：POST／GET ``/private_<token>/mcp/`` 回 307，
``Location: http://127.0.0.1:<子行程 port>/mcp``——露出內部 port，用戶端也會被導向它自己的 localhost。子行程（MCP SDK
``MCPServer``，正式的 odoo 子行程同一種）對 ``/mcp/`` 本來就回這個轉址，閘道原樣轉了出去。
"""
from __future__ import annotations

import json

import httpx
import pytest

from loopback_servers import ThreadedUvicorn, mcp_child_process

TOKEN = "trailing-slash-test-token-000000000000"  # 合成值
_ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
_INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "trailing-slash-test", "version": "0"}}}


def _sse_messages(body: str) -> list[dict]:
    out = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if data:
            out.append(json.loads(data))
    return out


def test_trailing_slash_reaches_the_real_mcp_child_without_redirect(tmp_config_path, monkeypatch):
    pytest.importorskip("mcp.server.mcpserver")
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app, proxy
    from managed_proxy_fixture import attach

    monkeypatch.setattr(proxy, "_client", None)
    with mcp_child_process() as (port, child):
        # 前提：子行程自己對 /mcp/ 回 307 指向 127.0.0.1（閘道以前原樣轉出去的就是這個）
        direct = httpx.post(f"http://127.0.0.1:{port}/mcp/", json=_INITIALIZE, headers=_ACCEPT)
        assert direct.status_code == 307 and direct.headers["location"].startswith("http://127.0.0.1:")

        attach(monkeypatch, child)
        config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        config.update(mcp_auth_token=TOKEN, mcp_server={"command": "", "args": [], "port": port, "env": {}})
        tmp_config_path.write_text(json.dumps(config))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        with ThreadedUvicorn(create_app(title="trailing slash")) as admin:
            url = f"http://127.0.0.1:{admin.port}/private_{TOKEN}/mcp/"
            with httpx.Client(timeout=20) as client:
                initialized = client.post(url, json=_INITIALIZE, headers=_ACCEPT)
                assert initialized.status_code == 200, initialized.headers.get("location")
                assert "location" not in initialized.headers
                session = initialized.headers["mcp-session-id"]
                headers = {**_ACCEPT, "mcp-session-id": session}
                assert client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                   headers=headers).status_code in (200, 202)
                listed = client.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
                assert listed.status_code == 200
                tools = [m for m in _sse_messages(listed.text) if m.get("id") == 2]
                assert "slow" in [tool["name"] for tool in tools[0]["result"]["tools"]]
                # 沒有斜線的寫法照舊
                plain = client.post(url.rstrip("/"), json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                                    headers=headers)
                assert plain.status_code == 200
        proxy._client = None


@pytest.mark.usefixtures("synthetic_child")
@pytest.mark.parametrize(("path", "child_location", "expected"), [
    # legacy SSE 的 /sse/ → 子行程轉到 /sse：相對於 /private_<token>/sse/ 是 ../sse
    ("sse/", "http://127.0.0.1:8123/sse", "../sse"),
    ("messages", "http://127.0.0.1:8123/messages/?session_id=abc", "messages/?session_id=abc"),
    ("sse/", "http://localhost:8123/sse", "../sse"),
    ("sse/", "http://[::1]:8123/sse", "../sse"),
    ("sse/", "/sse", "../sse"),
    ("a/b/c", "http://127.0.0.1:9999/", "../../"),
    ("x", "/../../outside", "outside"),  # 不會爬出 /private_<token>/
    ("sse/", "https://example.com/elsewhere", "https://example.com/elsewhere"),
    ("sse/", "relative/path", "relative/path"),
])
def test_child_redirect_location_is_rewritten_without_internal_address_or_token(
        make_app, monkeypatch, path, child_location, expected):
    from mcp_admin_core import proxy

    seen = []

    def child(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(307, headers={"location": child_location})

    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(child)))
    client = make_app({"mcp_auth_token": TOKEN,
                       "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}}})
    response = client.get(f"/private_{TOKEN}/{path}", follow_redirects=False)
    assert response.status_code == 307
    location = response.headers["location"]
    assert location == expected
    assert TOKEN not in location
    if "example.com" not in expected:
        assert "127.0.0.1" not in location and "localhost" not in location and "::1" not in location
        # 用戶端照 RFC 3986 解析，會回到同一個 /private_<token>/ 底下
        resolved = httpx.URL(f"https://tenant.example/private_{TOKEN}/{path}").join(location)
        assert resolved.path.startswith(f"/private_{TOKEN}/")


@pytest.mark.usefixtures("synthetic_child")
@pytest.mark.parametrize("path", ["mcp/", "mcp//", "/mcp"])
def test_streamable_endpoint_variants_are_forwarded_to_mcp(make_app, monkeypatch, path):
    from mcp_admin_core import proxy

    seen = []

    def child(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(child)))
    client = make_app({"mcp_auth_token": TOKEN,
                       "mcp_server": {"command": "", "args": [], "port": 8123, "env": {}}})
    response = client.post(f"/private_{TOKEN}/{path}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    assert seen == ["/mcp"]
