"""Real login/logout cookie behavior and deployment-only configuration."""
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.responses import Response

from mcp_admin_core.config import store as store_module
from mcp_admin_core import process as process_module
from headscale_mcp_admin import main
from headscale_mcp_admin.cookie_security import secure_cookie_enabled, secure_session_cookie


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("HEADSCALE_MCP_SECURE_COOKIE", raising=False)
    monkeypatch.setenv("HEADSCALE_MCP_ALLOWED_ORIGINS", "http://testserver,https://testserver")
    store = store_module.ConfigStore(tmp_path / "config.json")
    await store.put("admin_password", "secure-cookie-test-password")
    monkeypatch.setattr(store_module, "_instance", store)
    monkeypatch.setattr(process_module, "_instance", SimpleNamespace(is_running=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url="https://testserver") as client:
        yield client


def cookie(response):
    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    return parsed["mcp-admin-token"]


async def test_default_https_login_and_logout_preserve_cookie_attributes(client):
    login = await client.post("/api/auth/login", json={"password": "secure-cookie-test-password"})
    assert login.status_code == 200
    issued = cookie(login)
    assert issued["secure"] and issued["httponly"]
    assert issued["samesite"] == "strict" and issued["path"] == "/"
    assert int(issued["max-age"]) > 0
    assert (await client.get("/api/headscale/connection")).status_code == 200
    logout = await client.post("/api/auth/logout")
    assert logout.status_code == 200
    deleted = cookie(logout)
    assert deleted["secure"] and deleted["max-age"] == "0" and deleted["expires"]
    assert deleted["path"] == "/" and deleted["samesite"] == "lax"
    assert (await client.get("/api/headscale/connection")).status_code == 401


async def test_default_cookie_not_sent_over_http_or_downgraded_by_headers(client):
    client.base_url = "http://testserver"
    login = await client.post("/api/auth/login", json={"password": "secure-cookie-test-password"},
                              headers={"Origin": "http://testserver", "X-Forwarded-Proto": "http"})
    assert login.status_code == 200 and cookie(login)["secure"]
    assert (await client.get("/api/headscale/connection")).status_code == 401


async def test_explicit_false_allows_local_http_session_and_logout(client, monkeypatch):
    monkeypatch.setenv("HEADSCALE_MCP_SECURE_COOKIE", "false")
    client.base_url = "http://testserver"
    login = await client.post("/api/auth/login", json={"password": "secure-cookie-test-password"})
    assert login.status_code == 200 and not cookie(login)["secure"]
    assert cookie(login)["httponly"] and cookie(login)["samesite"] == "strict"
    assert (await client.get("/api/headscale/connection")).status_code == 200
    logout = await client.post("/api/auth/logout")
    assert logout.status_code == 200 and not cookie(logout)["secure"]
    assert cookie(logout)["max-age"] == "0"
    assert (await client.get("/api/headscale/connection")).status_code == 401


@pytest.mark.parametrize("value, expected", [("true", True), ("TRUE", True),
                                             (" false ", False), ("False", False)])
def test_explicit_boolean_values(monkeypatch, value, expected):
    monkeypatch.setenv("HEADSCALE_MCP_SECURE_COOKIE", value)
    assert secure_cookie_enabled() is expected


@pytest.mark.parametrize("value", ["", "1", "0", "yes", "no", "auto", "false-ish"])
async def test_invalid_flag_fails_startup_before_config_or_child(monkeypatch, value):
    monkeypatch.setenv("HEADSCALE_MCP_SECURE_COOKIE", value)
    store = Mock()
    child_start = AsyncMock()
    monkeypatch.setattr(main, "get_config_store", store)
    monkeypatch.setattr(process_module, "_instance", SimpleNamespace(start=child_start))
    with pytest.raises(RuntimeError, match="HEADSCALE_MCP_SECURE_COOKIE must be true or false"):
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("invalid flag reached application startup")
    store.assert_not_called()
    child_start.assert_not_called()


def test_multiple_cookie_headers_and_existing_attributes_preserved(monkeypatch):
    monkeypatch.delenv("HEADSCALE_MCP_SECURE_COOKIE", raising=False)
    response = Response()
    headers = [(b"content-type", b"application/json"),
               (b"set-cookie", b"other=value; Path=/; HttpOnly; SameSite=None"),
               (b"set-cookie", b"mcp-admin-token=example; Expires=Wed, 11 Nov 2026 00:00:00 GMT; Path=/; HttpOnly; SameSite=Strict"),
               (b"set-cookie", b"mcp-admin-token=deleted; Path=/old; Max-Age=0; secure"),
               (b"set-cookie", b"mcp-admin-token-extra=untouched; Path=/")]
    response.raw_headers = list(headers)
    secure_session_cookie(response)
    expected = list(headers)
    expected[2] = (headers[2][0], headers[2][1] + b"; Secure")
    assert response.raw_headers == expected
    secure_session_cookie(response)
    assert response.raw_headers == expected


def test_explicit_false_preserves_all_raw_headers(monkeypatch):
    monkeypatch.setenv("HEADSCALE_MCP_SECURE_COOKIE", "false")
    response = Response()
    response.set_cookie("mcp-admin-token", "example", secure=True)
    before = list(response.raw_headers)
    secure_session_cookie(response)
    assert response.raw_headers == before
