"""管理後台安全標頭回歸測試（#1372）。

- 管理頁／API／healthz／401 回應都要帶 X-Frame-Options、CSP frame-ancestors、
  nosniff、Referrer-Policy；
- /private_* MCP proxy 路徑不套用（避免干擾 SSE／串流與 MCP client）；
- 串流回應不被緩衝（純 ASGI middleware 逐塊放行）。
"""

from __future__ import annotations

import pytest

EXPECTED = {
    "strict-transport-security": "max-age=15552000",
    "permissions-policy": "geolocation=(), payment=(), usb=()",
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


def _assert_has_security_headers(resp) -> None:
    for k, v in EXPECTED.items():
        assert resp.headers.get(k) == v, f"{k} missing/wrong on {resp.request.url}"


def _assert_no_security_headers(resp) -> None:
    for k in EXPECTED:
        assert k not in resp.headers, f"{k} should not be set on {resp.request.url}"


def test_healthz_has_security_headers(make_app):
    c = make_app()
    _assert_has_security_headers(c.get("/healthz"))


def test_login_and_unauthorized_api_have_security_headers(make_app):
    c = make_app()
    # AuthMiddleware 擋下的 401 也要帶（middleware 在最外層）；先測 401（登入後 client 會帶 cookie）
    r = c.get("/api/settings")
    assert r.status_code == 401
    _assert_has_security_headers(r)
    _assert_has_security_headers(c.post("/api/auth/login", json={"password": "admin"}))


def test_spa_index_has_security_headers(tmp_config_path, monkeypatch, tmp_path):
    import json

    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient

    tmp_config_path.write_text(json.dumps(dict(store_mod._DEFAULT_CONFIG)))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    store_mod.get_config_store()

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html><body>admin</body></html>")
    (static / "assets" / "app.js").write_text("console.log(1)")

    from mcp_admin_core import create_app

    c = TestClient(create_app(title="t", static_dir=static))
    r = c.get("/settings")
    assert r.status_code == 200 and "admin" in r.text
    _assert_has_security_headers(r)
    _assert_has_security_headers(c.get("/assets/app.js"))


@pytest.mark.parametrize("path", ["/private_secret/mcp", "/private_secret", "/private_wrong/mcp"])
def test_mcp_proxy_paths_are_excluded(make_app, path):
    # Podman hosts may already run a service on the core default child port 8000.
    # Reserve a currently free loopback port so this test observes the proxy's
    # unavailable-upstream path rather than an unrelated host service.
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        child_port = probe.getsockname()[1]
    c = make_app({"mcp_auth_token": "secret", "mcp_server": {"port": child_port}})
    r = c.get(path)
    # 無上游（503/502；/mcp 的 GET 由子行程沒在跑時的佔位 MCP 回 405）或 token 錯（403）——重點是不帶管理頁安全標頭
    assert r.status_code in (403, 405, 502, 503)
    _assert_no_security_headers(r)


def test_existing_header_is_not_overridden():
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from fastapi.testclient import TestClient

    from mcp_admin_core.security_headers import SecurityHeadersMiddleware

    app = FastAPI()

    @app.get("/x")
    async def x():
        return PlainTextResponse("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

    app.add_middleware(SecurityHeadersMiddleware)
    r = TestClient(app).get("/x")
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_streaming_response_passes_through_chunks():
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient

    from mcp_admin_core.security_headers import SecurityHeadersMiddleware

    app = FastAPI()

    @app.get("/api/logs/stream")
    async def stream():
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    app.add_middleware(SecurityHeadersMiddleware)
    with TestClient(app).stream("GET", "/api/logs/stream") as r:
        chunks = [c for c in r.iter_text() if c]
        _assert_has_security_headers(r)
    assert "".join(chunks) == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
