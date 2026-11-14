"""Exercise the public proxy boundary against an isolated HTTP upstream."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

import mcp_admin_core.process as process_module
from mcp_admin_core.config import store as store_module
from headscale_mcp_admin.main import app


def mounted_transport(mode):
    if mode == "root_path":
        return httpx.ASGITransport(app=app, root_path="/gateway"), "/gateway"
    if mode == "root_path_stripped":
        return httpx.ASGITransport(app=app, root_path="/gateway"), ""
    if mode == "mount":
        return httpx.ASGITransport(app=Starlette(routes=[Mount("/gateway", app=app)])), "/gateway"
    if mode == "root_path_mount":
        parent = Starlette(routes=[Mount("/gateway", app=app)])
        return httpx.ASGITransport(app=parent, root_path="/outer"), "/outer/gateway"
    child = Starlette(routes=[Mount("/gateway", app=app)])
    return httpx.ASGITransport(app=Starlette(routes=[Mount("/outer", app=child)])), "/outer/gateway"


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
    # The gateway forwards only to a manager generation whose listener passed its OS identity check. This
    # isolated upstream is started by the test, not by the manager: stand in for that check explicitly.
    manager = process_module.McpProcessManager()
    manager._process = SimpleNamespace(pid=os.getpid(), returncode=None)
    manager._running = True
    manager._peer_verified = (manager._initialize_generation_changed, upstream.server_port)
    monkeypatch.setattr(process_module, "_instance", manager)
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


@pytest.mark.parametrize("mode", ["root_path", "root_path_stripped", "mount", "nested_mount", "root_path_mount"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "CUSTOM"])
async def test_headscale_mounted_mcp_rejects_untrusted_origin_for_all_methods(proxy_client, mode, method):
    _, received = proxy_client
    transport, prefix = mounted_transport(mode)
    async with httpx.AsyncClient(transport=transport, base_url="https://admin.example") as client:
        response = await client.request(method, prefix + "/private_origin-fixture-token/mcp",
                                        headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert not received


@pytest.mark.parametrize("mode", ["root_path", "root_path_stripped", "mount", "nested_mount", "root_path_mount"])
async def test_headscale_mounted_admin_boundary_and_preflight(proxy_client, mode):
    _, received = proxy_client
    transport, prefix = mounted_transport(mode)
    async with httpx.AsyncClient(transport=transport, base_url="https://admin.example") as client:
        endpoint = prefix + "/private_origin-fixture-token/mcp"
        bad = await client.options(endpoint, headers={"Origin": "https://evil.example",
                                   "Access-Control-Request-Method": "POST"})
        assert bad.status_code == 403
        good = await client.options(endpoint, headers={"Origin": "https://admin.example",
                                    "Access-Control-Request-Method": "POST"})
        assert good.status_code == 200
        assert (await client.get(endpoint)).status_code == 200
        assert (await client.get(prefix + "/api/headscale/connection")).status_code == 401
        assert (await client.get(prefix + "/api/settings")).status_code == 404
        login = await client.post(prefix + "/api/auth/login", headers={"Origin": "https://evil.example"},
                                  json={"password": "synthetic"})
        assert login.status_code == 403
    assert len(received) == 1


async def test_legacy_trailing_dot_allowlist_keeps_exact_headscale_semantics(proxy_client, monkeypatch):
    from mcp_admin_core.origin_security import validate_origin_policy

    client, received = proxy_client
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_ORIGINS", "https://admin.example.")
    validate_origin_policy("HEADSCALE_MCP_ALLOWED_ORIGINS")
    endpoint = "/private_origin-fixture-token/mcp"
    assert (await client.get(endpoint)).status_code == 200
    assert (await client.get(endpoint, headers={"Origin": "https://admin.example."})).status_code == 200
    assert (await client.get(endpoint, headers={"Origin": "https://admin.example"})).status_code == 403
    result = await client.options(endpoint, headers={"Origin": "https://admin.example.",
                                  "Access-Control-Request-Method": "POST"})
    assert result.status_code == 200
    assert result.headers["access-control-allow-origin"] == "https://admin.example."
    assert len(received) == 2


@pytest.mark.parametrize("value", [
    "https://admin.example", "https://ADMIN.EXAMPLE:443", "https://admin.example.",
    "https://admin.example..", "http://127.0.0.1:19984", "http://[::1]:19984",
    "https://.", "https://a..example", "https://admin.example:0",
    "https://admin.example:65536", "https://*.example", "null",
    "https://user@admin.example", "https://admin.example/",
])
def test_common_headscale_origin_parser_matches_published_parser(value):
    from headscale_mcp_admin.origin_security import normalize_origin as published
    from mcp_admin_core.origin_security import normalize_origin as common

    try:
        expected = published(value)
    except ValueError:
        with pytest.raises(ValueError):
            common(value, headscale_legacy=True)
    else:
        assert common(value, headscale_legacy=True) == expected


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "CUSTOM"])
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


async def test_denied_mcp_origin_is_logged_once_without_path_or_token(proxy_client, monkeypatch, caplog):
    """Headscale 的 admin_boundary 在共用層之外先回 403：也要記同一行（只有原因、來源與 env 名稱）。"""
    from mcp_admin_core import origin_security

    monkeypatch.setattr(origin_security, "_DENIAL_LIMITER", origin_security.DenialLogLimiter())
    client, received = proxy_client
    response = await client.post("/private_origin-fixture-token/mcp?token=query-fixture-value",
        headers={"Origin": "https://evil.example", "Cookie": "sid=cookie-fixture-value"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response.status_code == 403 and response.json() == {"detail": "ORIGIN_DENIED"}
    assert not [name for name in response.headers if name.lower().startswith("access-control-")]
    lines = [r.getMessage() for r in caplog.records if r.name == "mcp_admin_core.origin_security"]
    assert lines == ['MCP Origin 被拒（403 ORIGIN_DENIED） decision=deny mode=enforce reason=not_allowed '
                     'origin="https://evil.example" env=HEADSCALE_MCP_ALLOWED_ORIGINS']
    for secret in ("origin-fixture-token", "private_", "query-fixture-value", "cookie-fixture-value"):
        assert secret not in lines[0]
    # 管理 API 的跨來源寫入照樣 403，但不是 MCP 端點：不記。允許的來源與沒有 Origin 也不記。
    assert (await client.post("/api/config/connection", headers={"Origin": "https://evil.example"},
                              json={})).status_code == 403
    await client.post("/private_origin-fixture-token/mcp", headers={"Origin": "https://admin.example"},
                      json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    await client.post("/private_origin-fixture-token/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert len([r for r in caplog.records if r.name == "mcp_admin_core.origin_security"]) == 1
    assert not received or all(b"initialize" not in body for _, _, body in received)


@pytest.mark.parametrize("mode", ["report-only", "", "enforce", "synthetic-invalid-mode-value"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "OPTIONS"])
async def test_headscale_ignores_mcp_admin_origin_mode_and_still_returns_403(proxy_client, monkeypatch, caplog,
                                                                             mode, method):
    """MCP_ADMIN_ORIGIN_MODE（其他產品預設 report-only、不擋）對 Headscale 沒有作用：它在 #136 之前就擋不受信任的
    Origin，這次不能放寬。不論設成什麼（含不合法的值），沒通過的 Origin 一律 403、不轉發、不帶 CORS 標頭。"""
    from mcp_admin_core import origin_security

    monkeypatch.setattr(origin_security, "_DENIAL_LIMITER", origin_security.DenialLogLimiter())
    monkeypatch.setenv("MCP_ADMIN_ORIGIN_MODE", mode)
    client, received = proxy_client
    assert origin_security.origin_mode(origin_security.HEADSCALE_ENV) == "enforce"
    extra = {"Access-Control-Request-Method": "POST"} if method == "OPTIONS" else {}
    for headers in ([("Origin", "https://evil.example")], [("Origin", "null")], [("Origin", "https://admin.example/")],
                    [("Origin", "https://evil.example"), ("Origin", "https://admin.example")]):
        response = await client.request(method, "/private_origin-fixture-token/mcp",
                                        headers=headers + list(extra.items()))
        assert response.status_code == 403 and response.json() == {"detail": "ORIGIN_DENIED"}
        assert not [name for name in response.headers if name.lower().startswith("access-control-")]
    assert not received
    lines = [r.getMessage() for r in caplog.records if r.name == "mcp_admin_core.origin_security"]
    assert len(lines) == 4 and all(" decision=deny mode=enforce " in line for line in lines)
    assert not any("would_deny" in line or "origin-fixture-token" in line for line in lines)
    # 清單內的來源照常（證明上面的 403 不是因為整個服務壞了）。
    good = await client.request(method, "/private_origin-fixture-token/mcp",
                                headers={"Origin": "https://admin.example", **extra},
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if method == "POST" else None)
    assert good.status_code == 200
