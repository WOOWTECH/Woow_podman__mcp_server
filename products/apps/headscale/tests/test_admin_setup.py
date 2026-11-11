"""Product boundary tests: real auth, storage and policy; isolated upstream."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mcp_admin_core.config import store as store_module
from mcp_admin_core import process as process_module
from headscale_mcp_admin.main import app
from headscale_mcp_admin import management


@pytest.fixture
async def admin(tmp_path, monkeypatch):
    monkeypatch.delenv("HEADSCALE_MCP_SECURE_COOKIE", raising=False)
    store = store_module.ConfigStore(tmp_path / "config.json")
    monkeypatch.setattr(store_module, "_instance", store)
    monkeypatch.setattr(process_module, "_instance", SimpleNamespace(is_running=False, start_or_restart=AsyncMock(return_value=True)))
    await store.put("admin_password", "test-management-password")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as c:
        yield c, store


async def login(c):
    r = await c.post("/api/auth/login", json={"password": "test-management-password"})
    assert r.status_code == 200


async def revision(c):
    r = await c.get("/api/headscale/connection")
    assert r.status_code == 200
    return {"If-Match": r.headers["ETag"]}


@pytest.mark.asyncio
async def test_auth_and_origin_boundary(admin):
    c, store = admin
    assert (await c.get("/api/headscale/connection")).status_code == 401
    assert (await c.get("/api/headscale/catalog")).status_code == 401
    assert (await c.get("/")).status_code == 200
    await login(c)
    r = await c.put("/api/headscale/connection", headers={"Origin": "https://attacker.example"}, json={"base_url": "https://hs.example"})
    assert r.status_code == 403
    assert not await store.get("connection")
    assert (await c.put("/api/settings/connection", json={"base_url": "https://wrong.example"})).status_code in (404,405)
    await c.post("/api/auth/logout")
    assert (await c.get("/api/headscale/catalog")).status_code == 401


@pytest.mark.asyncio
async def test_credentials_revision_and_new_origin(admin):
    c, store = admin
    await login(c)
    payload = {"base_url": "https://hs.example/api/v1", "api_key": {"action": "replace", "value": "credential-unique-test"}, "apply": False}
    assert (await c.put("/api/headscale/connection", json=payload)).status_code == 428
    headers = await revision(c)
    r = await c.put("/api/headscale/connection", headers=headers, json=payload)
    assert r.status_code == 200 and r.json()["saved"]
    assert "credential-unique-test" not in r.text
    assert (await c.put("/api/headscale/connection", headers=headers, json=payload)).status_code == 409
    r = await c.get("/api/headscale/connection")
    assert r.json()["credentials"]["api_key"] == {"set": True}
    assert "credential-unique-test" not in r.text
    payload = {"base_url": "https://hs.example", "apply": False}
    assert (await c.put("/api/headscale/connection", headers=await revision(c), json=payload)).status_code == 200
    assert (await store.get("connection"))["api_key"] == "credential-unique-test"
    payload["base_url"] = "https://different.example"
    assert (await c.put("/api/headscale/connection", headers=await revision(c), json=payload)).status_code == 422
    assert (await store.get("connection"))["base_url"] == "https://hs.example"
    payload["api_key"] = {"action": "clear"}
    assert (await c.put("/api/headscale/connection", headers=await revision(c), json=payload)).status_code == 200
    reopened = store_module.ConfigStore(store.path)
    assert (await reopened.get("connection"))["api_key"] == ""
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_validation_never_reflects_secret(admin):
    c, _ = admin
    await login(c)
    secret = "VERY_SECRET_VALUE with_newline\n"
    r = await c.put("/api/headscale/connection", headers=await revision(c), json={"base_url": "https://hs.example", "api_key": {"action": "replace", "value": secret}})
    assert r.status_code == 422 and "VERY_SECRET_VALUE" not in r.text
    r = await c.put("/api/headscale/connection", headers=await revision(c), json={"base_url": "https://user:password@hs.example"})
    assert r.status_code == 422 and "password@" not in r.text


@pytest.mark.asyncio
async def test_spawn_acknowledgement_is_not_applied_configuration(admin):
    c, _ = admin
    await login(c)
    r = await c.put("/api/headscale/connection", headers=await revision(c), json={
        "base_url": "https://hs.example", "api_key": {"action": "replace", "value": "fixture-key"}})
    assert r.status_code == 200
    assert r.json()["saved"] is True
    assert r.json()["configuration"]["apply_state"] != "applied"
    assert r.json()["applied"] is False


@pytest.mark.asyncio
async def test_disabled_tool_has_no_upstream_effect(admin, monkeypatch):
    c, store = admin
    await login(c)
    await store.put("tools", {"disabled": ["headscale_users"], "disabled_operations": {}})
    upstream = AsyncMock()
    monkeypatch.setattr(management, "client_from_saved", upstream)
    r = await c.post("/api/headscale/tools/headscale_users", json={"arguments": {"operation": "create", "name": "must-not-exist"}})
    assert r.status_code == 403
    upstream.assert_not_called()
    catalog = (await c.get("/api/headscale/catalog")).json()
    assert next(t for t in catalog["tools"] if t["name"] == "headscale_users")["enabled"] is False


@pytest.mark.asyncio
async def test_chunked_body_limit(admin):
    c, _ = admin
    await login(c)
    async def chunks():
        yield b'{"arguments":{"value":"'
        yield b"x" * (1024 * 1024)
        yield b'"}}'
    r = await c.post("/api/headscale/tools/headscale_health", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
