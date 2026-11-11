"""Exercise the public proxy boundary against an isolated HTTP upstream."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from mcp_admin_core.config import store as store_module
from headscale_mcp_admin.main import app


@pytest.fixture
async def proxy_client(tmp_path, monkeypatch):
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.command, self.path, body))
            rpc = json.loads(body) if body else {}
            payload = json.dumps({"jsonrpc": "2.0", "id": rpc.get("id", 1),
                                  "result": {"tools": []}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = answer

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    store = store_module.ConfigStore(tmp_path / "config.json")
    await store.put("mcp_auth_token", "origin-fixture-token")
    await store.put("mcp_server", {"port": upstream.server_port})
    monkeypatch.setattr(store_module, "_instance", store)
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_ORIGINS", "https://admin.example")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://admin.example") as client:
            yield client, received
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join()


async def test_untrusted_origin_cannot_initialize_with_valid_mcp_token(proxy_client):
    client, received = proxy_client
    response = await client.post("/private_origin-fixture-token/mcp",
        headers={"Origin": "https://evil.example"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response.status_code == 403
    assert not received


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
@pytest.mark.parametrize("suffix", ["", "/mcp", "/sse", "/messages/?session_id=existing"])
async def test_all_public_mcp_methods_reject_before_forwarding(proxy_client, method, suffix):
    client, received = proxy_client
    response = await client.request(method, "/private_origin-fixture-token" + suffix,
                                    headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert not received


@pytest.mark.parametrize("headers", [
    [("Origin", "")], [("Origin", "null")], [("Origin", "https://admin.example/")],
    [("Origin", "https://admin.example?x")], [("Origin", "https://admin.example#x")],
    [("Origin", "https://user@admin.example")], [("Origin", "https://admin.example:bad")],
    [("Origin", "https://admin.example:65536")], [("Origin", "https://admin.example:0")],
    [("Origin", "https://admin.example,https://evil.example")],
    [("Origin", "https://admin.example https://evil.example")],
    [("Origin", "https://admin.example"), ("Origin", "https://admin.example")],
    [("Origin", "https://admin.example"), ("Origin", "https://evil.example")],
    [("Origin", "https://admin.example.evil.example")], [("Origin", "http://admin.example")],
    [("Origin", "https://admin.example:444")], [("Origin", "https://admin.example.")],
    [("Origin", " https://admin.example")], [("Origin", "https://admin.example\t")],
    [("Origin", "https://evil.example"), ("Host", "evil.example"),
     ("X-Forwarded-Host", "evil.example"), ("Forwarded", "host=evil.example;proto=https")],
])
async def test_bad_origins_never_inherit_host_or_forwarded_trust(proxy_client, headers):
    client, received = proxy_client
    assert (await client.get("/private_origin-fixture-token/mcp", headers=headers)).status_code == 403
    assert not received


@pytest.mark.parametrize("origin", [None, "https://admin.example", "HTTPS://ADMIN.EXAMPLE:443"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"])
async def test_allowed_and_native_clients_reach_upstream(proxy_client, origin, method):
    client, received = proxy_client
    response = await client.request(method, "/private_origin-fixture-token/mcp",
        headers={"Origin": origin} if origin is not None else {},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    assert len(received) == 1


async def test_allowlist_is_explicit_and_invalid_tokens_still_denied(proxy_client, monkeypatch):
    client, received = proxy_client
    for origins in ("", "https://different.example"):
        monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_ORIGINS", origins)
        assert (await client.get("/private_origin-fixture-token/mcp",
                                headers={"Origin": "https://admin.example"})).status_code == 403
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_ORIGINS", "http://127.0.0.1:19984, https://admin.example")
    for origin in ("http://127.0.0.1:19984", "https://admin.example"):
        assert (await client.get("/private_wrong/mcp", headers={"Origin": origin})).status_code == 403
        assert (await client.get("/private_origin-fixture-token/mcp", headers={"Origin": origin})).status_code == 200
    assert len(received) == 2


@pytest.mark.parametrize("origin", ["*", "null", "https://admin.example/path", "https://admin.example,", "https://*.example"])
async def test_invalid_allowlist_fails_closed(proxy_client, monkeypatch, origin):
    from headscale_mcp_admin.origin_security import allowed_origins
    client, received = proxy_client
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_ORIGINS", origin)
    assert (await client.get("/private_origin-fixture-token/mcp")).status_code == 503
    with pytest.raises(RuntimeError, match="HEADSCALE_MCP_ALLOWED_ORIGINS"):
        allowed_origins()
    assert not received


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_mutable_api_boundary_uses_same_allowlist(proxy_client, method):
    client, _ = proxy_client
    for path in ("/api/auth/login", "/api/auth/logout", "/api/headscale/connection"):
        assert (await client.request(method, path, headers={"Origin": "https://evil.example", "Host": "evil.example"})).status_code == 403
        assert (await client.request(method, path, headers={"Origin": "null"})).status_code == 403
        assert (await client.request(method, path, headers={"Origin": ""})).status_code == 403


async def test_trusted_browser_can_login_write_and_logout(proxy_client):
    client, _ = proxy_client
    await store_module.get_config_store().put("admin_password", "origin-test-password")
    client.headers["Origin"] = "https://admin.example"
    assert (await client.post("/api/auth/login", json={"password": "origin-test-password"})).status_code == 200
    before = await client.get("/api/headscale/connection")
    assert before.status_code == 200
    saved = await client.put("/api/headscale/connection", headers={"If-Match": before.headers["etag"]},
                            json={"base_url": "https://headscale.example", "apply": False})
    assert saved.status_code == 200
    assert (await client.post("/api/auth/logout")).status_code == 200
