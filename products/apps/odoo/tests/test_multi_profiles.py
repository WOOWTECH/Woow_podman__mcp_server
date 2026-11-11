"""Five accounts on one Odoo database remain bound to five profile URLs."""

from __future__ import annotations

import asyncio
import json
import os

from fastapi.testclient import TestClient

import mcp_admin_core.config.store as store_mod
import mcp_admin_core.profiles as profiles_mod
from mcp_admin_core.launch_policy import set_launch_policy
from mcp_admin_core.process import McpProcessManager
from mcp_admin_core.profiles import get_profile_registry
from management_client import revision_headers
from odoo_mcp_admin.main import LAUNCH_POLICY, app
import odoo_mcp_admin.routers.config as config_router


def test_five_odoo_accounts_same_database_are_fixed_to_separate_profiles(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    seed = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    seed["connection"] = {"odoo_url": "https://odoo.example", "odoo_db": "shared",
                          "odoo_username": "legacy", "odoo_password": "legacy-key"}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(seed, stream)
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
    monkeypatch.setattr(store_mod, "_instance", None)
    set_launch_policy(LAUNCH_POLICY)
    monkeypatch.setattr(profiles_mod, "_memory_limit_mib", lambda: 1024)

    async def no_child(_self):
        return False

    monkeypatch.setattr(McpProcessManager, "start", no_child)

    class OdooCommon:
        def version(self):
            return {"server_version": "18.0"}

        def authenticate(self, database, credentials, _agent):
            assert database == "shared"
            username = credentials["login"]
            number = int(username.removeprefix("user-"))
            assert credentials["password"] == f"key-{number}"
            return {"uid": 100 + number}

    def proxy(url):
        assert url == "https://odoo.example/xmlrpc/2/common"
        return OdooCommon()

    monkeypatch.setattr(config_router, "make_proxy", proxy)
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"password": "admin"})
    assert login.status_code == 200
    auth = {"Authorization": f"Bearer {login.json()['token']}"}

    profiles = []
    for number in range(1, 6):
        created = client.post("/api/profiles", json={"label": f"User {number}"}, headers=auth)
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]
        selected = {**auth, "X-MCP-Profile": profile_id}
        payload = {"odoo_url": "https://odoo.example", "odoo_db": "shared",
                   "odoo_username": f"user-{number}", "odoo_password": f"key-{number}",
                   "restart": False}
        saved = client.put("/api/config/connection", json=payload,
                           headers=revision_headers(client, selected, read_path="/api/config"))
        assert saved.status_code == 200, saved.text
        token = client.post("/api/settings/mcp_auth_token/reveal", headers=selected)
        assert token.status_code == 200
        profiles.append((profile_id, token.json()["value"], number))

    assert len({token for _, token, _ in profiles}) == 5
    assert len({row["port"] for row in client.get("/api/profiles", headers=auth).json()["profiles"][1:]}) == 5
    for profile_id, token, number in reversed(profiles):
        selected = {**auth, "X-MCP-Profile": profile_id}
        result = client.post("/api/config/test", headers=selected)
        assert result.status_code == 200, result.text
        assert result.json()["success"] is True
        assert result.json()["uid"] == 100 + number
        stored = asyncio.run(get_profile_registry().store_for(profile_id).load())
        assert stored["connection"]["odoo_db"] == "shared"
        assert stored["connection"]["odoo_username"] == f"user-{number}"
        assert stored["connection"]["odoo_password"] == f"key-{number}"
        assert token == stored["mcp_auth_token"]

    default = client.get("/api/config", headers=auth).json()
    assert default["odoo_username"] == "legacy"
