"""Dashboard sessions must follow the selected profile and its credentials."""

import json

import httpx
import pytest

from hermes_mcp_admin import dashboard_auth
from hermes_mcp_admin.routers import dashboard_proxy, health
from mcp_admin_core.config.store import ConfigStore
from mcp_admin_core.profiles import ProfileRegistry


def _dashboard_transport(monkeypatch):
    real_client = httpx.AsyncClient
    logins = []

    def respond(request):
        if request.url.path == "/auth/password-login":
            username = json.loads(request.content)["username"]
            logins.append(username)
            return httpx.Response(200, headers={
                "set-cookie": f"hermes_session_at=session-{username}; Path=/",
            })
        cookie = request.headers.get("cookie", "")
        owner = cookie.partition("session-")[2].partition('"')[0]
        return httpx.Response(200, json={"owner": owner})

    def client(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return logins


async def _profiles(tmp_path):
    registry = ProfileRegistry(ConfigStore(tmp_path / "config.json"))
    first = (await registry.create("First"))["id"]
    second = (await registry.create("Second"))["id"]
    url = f"https://dashboard-{tmp_path.name}.invalid"
    for profile_id, username in ((first, "alice"), (second, "bob")):
        await registry.store_for(profile_id).patch("connection", {
            "dashboard_url": url,
            "dashboard_username": username,
            "dashboard_password": f"password-{username}",
        })
    return registry, first, second, url


@pytest.mark.asyncio
async def test_dashboard_proxy_cookie_follows_profile_and_credential_changes(tmp_path, monkeypatch):
    registry, first, second, _url = await _profiles(tmp_path)
    logins = _dashboard_transport(monkeypatch)

    with registry.activate(first):
        assert (await dashboard_proxy._dashboard_get("/api/skills"))["owner"] == "alice"
    with registry.activate(second):
        assert (await dashboard_proxy._dashboard_get("/api/skills"))["owner"] == "bob"
        assert (await dashboard_proxy._dashboard_put("/api/skills", {"name": "new"}))["owner"] == "bob"
    with registry.activate(first):
        assert (await dashboard_proxy._dashboard_get("/api/skills"))["owner"] == "alice"
    assert logins == ["alice", "bob"]

    await registry.store_for(first).patch("connection", {
        "dashboard_username": "alice-rotated",
        "dashboard_password": "password-alice-rotated",
    })
    with registry.activate(first):
        assert (await dashboard_proxy._dashboard_get("/api/skills"))["owner"] == "alice-rotated"
    assert logins == ["alice", "bob", "alice-rotated"]

    dashboard_auth.remove_profile(first)
    with registry.activate(first):
        assert (await dashboard_proxy._dashboard_get("/api/skills"))["owner"] == "alice-rotated"
    assert logins == ["alice", "bob", "alice-rotated", "alice-rotated"]


@pytest.mark.asyncio
async def test_dashboard_health_cookie_follows_profile(tmp_path, monkeypatch):
    registry, first, second, url = await _profiles(tmp_path)
    logins = _dashboard_transport(monkeypatch)

    with registry.activate(first):
        assert (await health._check_dashboard(url, "alice", "password-alice"))["_status"]["owner"] == "alice"
        assert (await health._check_dashboard(url, "alice", "password-alice"))["_status"]["owner"] == "alice"
    with registry.activate(second):
        assert (await health._check_dashboard(url, "bob", "password-bob"))["_status"]["owner"] == "bob"
    assert logins == ["alice", "bob"]
