"""Five fixed backend identities behind one product admin and five MCP URLs."""

from __future__ import annotations

import asyncio
import json
import stat

import httpx
import pytest

from mcp_admin_core.profiles import ProfileRegistry, get_profile_registry


@pytest.fixture(autouse=True)
def _one_gib_test_container(monkeypatch):
    # CI runner cgroup limits vary; these tests assert the 1 GiB product
    # contract except the dedicated 512 MiB/2 GiB capacity cases.
    monkeypatch.setattr("mcp_admin_core.profiles._memory_limit_mib", lambda: 1024)


def _admin(client):
    login = client.post("/api/auth/login", json={"password": "admin"})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["token"]}


def test_five_profiles_have_private_independent_configs_and_urls(make_app, monkeypatch):
    client = make_app({
        "mcp_auth_token": "default-secret",
        "connection": {"odoo_url": "https://odoo.example", "odoo_db": "one-db",
                       "odoo_username": "legacy", "odoo_password": "legacy-key"},
        "proxy": {"timeout": 1800, "bearer_token": "default-only-bearer"},
        "mcp_server": {"command": "", "args": [], "port": 59999, "env": {}},
    })
    assert client.get("/api/profiles").status_code == 401
    auth = _admin(client)
    assert client.get("/api/profiles", headers=auth).json()["profiles"][0]["id"] == "default"

    created = []
    tokens = []
    for i in range(5):
        response = client.post("/api/profiles", headers=auth, json={"label": f"Odoo user {i + 1}"})
        assert response.status_code == 201, response.text
        profile = response.json()
        created.append(profile)
        selected = {**auth, "X-MCP-Profile": profile["id"]}
        token = client.post("/api/settings/mcp_auth_token/reveal", headers=selected).json()["value"]
        assert token
        tokens.append(token)

    assert len({p["port"] for p in created}) == 5
    assert len(set(tokens)) == 5
    assert client.post("/api/profiles", headers=auth, json={"label": "Odoo user 6"}).status_code == 409
    registry = get_profile_registry()
    for i, profile in enumerate(created):
        store = registry.store_for(profile["id"])
        cfg = asyncio.run(store.load())
        assert cfg["connection"] == {"odoo_url": "", "odoo_db": "", "odoo_username": "", "odoo_password": ""}
        assert cfg["mcp_server"]["port"] == profile["port"]
        assert cfg["proxy"]["timeout"] == 1800
        assert not cfg["proxy"].get("bearer_token")
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.path.parent.stat().st_mode) & 0o777 == 0o700
        assert token not in json.dumps(asyncio.run(registry.default_store.get("profile_index", {})))

    seen = []
    real_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: (
        seen.append((str(req.url), req.headers.get("authorization"), req.headers.get("x-mcp-profile")))
        or httpx.Response(200, json={"ok": True})
    )))
    import mcp_admin_core.proxy as proxy
    monkeypatch.setattr(proxy, "_get_client", lambda timeout=86400: real_client)
    for i, token in enumerate(tokens):
        response = client.get(f"/private_{token}/health", headers={
            "Authorization": "Bearer untrusted-upstream-token", "X-MCP-Profile": created[(i + 1) % 5]["id"]})
        assert response.status_code == 200
        assert seen[-1] == (f"http://127.0.0.1:{created[i]['port']}/health", None, None)
    assert client.get("/private_default-secret/health").status_code == 200
    assert seen[-1][0] == "http://127.0.0.1:59999/health"

    assert client.get("/api/tokens", headers={**auth, "X-MCP-Profile": "unknown"}).status_code == 404
    assert client.get("/private_unknown/health").status_code == 403
    assert client.get("/api/profiles", headers=auth).json()["profiles"] == [
        {"id": "default", "label": "Default", "port": 59999, "is_default": True}, *created]


def test_profile_token_revocation_is_local(make_app):
    client = make_app({"mcp_auth_token": "default-secret", "mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    a = client.post("/api/profiles", headers=auth, json={"label": "A"}).json()["id"]
    b = client.post("/api/profiles", headers=auth, json={"label": "B"}).json()["id"]
    get_token = lambda pid: client.post("/api/settings/mcp_auth_token/reveal", headers={**auth, "X-MCP-Profile": pid}).json()["value"]
    a_token, b_token = get_token(a), get_token(b)
    store = get_profile_registry().store_for(a)
    asyncio.run(store.change_mcp_token(None, action="revoke"))
    assert client.get(f"/private_{a_token}/health").status_code == 403
    assert client.get(f"/private_{b_token}/health").status_code == 503
    assert client.get("/private_default-secret/health").status_code == 503
    assert client.post("/api/tokens", headers={**auth, "X-MCP-Profile": "unknown"}).status_code == 404


def test_token_cannot_be_assigned_to_two_profiles(make_app):
    from management_client import revision_headers

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    first = client.post("/api/profiles", headers=auth, json={"label": "first"}).json()["id"]
    second = client.post("/api/profiles", headers=auth, json={"label": "second"}).json()["id"]
    first_token = asyncio.run(get_profile_registry().store_for(first).get("mcp_auth_token"))
    second_auth = {**auth, "X-MCP-Profile": second}
    rejected = client.put("/api/tokens", json={"token": first_token},
                          headers=revision_headers(client, second_auth, read_path="/api/tokens"))
    assert rejected.status_code == 409
    assert asyncio.run(get_profile_registry().store_for(second).get("mcp_auth_token")) != first_token


def test_concurrent_manual_token_claims_have_one_owner(make_app, monkeypatch):
    from fastapi import HTTPException

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    first = client.post("/api/profiles", headers=auth, json={"label": "first"}).json()["id"]
    second = client.post("/api/profiles", headers=auth, json={"label": "second"}).json()["id"]
    registry = get_profile_registry()
    token = "one-manual-token-with-enough-entropy-for-test"
    for profile_id in (first, second):
        store = registry.store_for(profile_id)
        original = store.change_mcp_token

        async def delayed(value, *, action, original=original):
            await asyncio.sleep(0.02)
            return await original(value, action=action)

        monkeypatch.setattr(store, "change_mcp_token", delayed)

    async def claims():
        return await asyncio.gather(
            registry.assign_token(first, token), registry.assign_token(second, token),
            return_exceptions=True,
        )

    results = asyncio.run(claims())
    assert len([result for result in results if result is None]) == 1
    assert len([result for result in results if isinstance(result, HTTPException) and result.status_code == 409]) == 1
    assert asyncio.run(registry.resolve_token(token)) in {first, second}


def test_damaged_profile_reserves_unknown_token_against_manual_claim(make_app, monkeypatch):
    import mcp_admin_core.profiles as profiles
    from management_client import revision_headers

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    healthy = client.post("/api/profiles", headers=auth, json={"label": "healthy"}).json()["id"]
    damaged = client.post("/api/profiles", headers=auth, json={"label": "damaged"}).json()["id"]
    registry = get_profile_registry()
    path = registry.store_for(damaged).path
    saved_bytes = path.read_bytes()
    damaged_token = asyncio.run(registry.store_for(damaged).get("mcp_auth_token"))
    path.unlink()
    monkeypatch.setattr(profiles, "_registry", ProfileRegistry(registry.default_store))

    selected = {**auth, "X-MCP-Profile": healthy}
    rejected = client.put("/api/tokens", json={"token": damaged_token},
                          headers=revision_headers(client, selected, read_path="/api/tokens"))
    assert rejected.status_code == 503, rejected.text
    path.write_bytes(saved_bytes)
    monkeypatch.setattr(profiles, "_registry", ProfileRegistry(registry.default_store))
    assert asyncio.run(get_profile_registry().resolve_token(damaged_token)) == damaged


def test_deleting_profile_removes_child_database_directory(make_app):
    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    profile_id = client.post("/api/profiles", headers=auth, json={"label": "n8n account"}).json()["id"]
    registry = get_profile_registry()
    directory = registry.store_for(profile_id).path.parent
    database = directory / "n8n-mcp"
    database.mkdir()
    (database / "nodes.db").write_bytes(b"synthetic database")
    token = asyncio.run(registry.store_for(profile_id).get("mcp_auth_token"))

    response = client.delete(f"/api/profiles/{profile_id}", headers=auth)
    assert response.status_code == 204
    assert not directory.exists()
    assert client.get(f"/private_{token}/mcp").status_code == 403


def test_missing_extra_profile_does_not_disable_other_tokens_or_startup(make_app, monkeypatch):
    import mcp_admin_core.profiles as profiles
    from mcp_admin_core.process import McpProcessManager

    client = make_app({"mcp_auth_token": "default-secret",
                       "mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    healthy = client.post("/api/profiles", headers=auth, json={"label": "healthy"}).json()["id"]
    damaged = client.post("/api/profiles", headers=auth, json={"label": "damaged"}).json()["id"]
    registry = get_profile_registry()
    healthy_token = asyncio.run(registry.store_for(healthy).get("mcp_auth_token"))
    damaged_token = asyncio.run(registry.store_for(damaged).get("mcp_auth_token"))
    damaged_dir = registry.store_for(damaged).path.parent
    (damaged_dir / "config.json").unlink()
    monkeypatch.setattr(profiles, "_registry", ProfileRegistry(registry.default_store))

    assert client.get("/private_default-secret/health").status_code == 503
    assert client.get(f"/private_{healthy_token}/health").status_code == 503
    assert client.get(f"/private_{damaged_token}/health").status_code == 403

    starts = []

    async def fake_start(self):
        starts.append(self)
        return True

    monkeypatch.setattr(McpProcessManager, "start", fake_start)
    reloaded = get_profile_registry()
    asyncio.run(reloaded.start_all())
    assert starts == [reloaded.manager_for(healthy)]
    assert client.delete(f"/api/profiles/{damaged}", headers=auth).status_code == 204
    assert not damaged_dir.exists()


def test_fs_group_widened_profile_directories_are_tightened_on_reload(make_app):
    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    profile_id = client.post("/api/profiles", headers=auth, json={"label": "person"}).json()["id"]
    registry = get_profile_registry()
    directory = registry.store_for(profile_id).path.parent
    directory.parent.chmod(0o2770)
    directory.chmod(0o2770)

    reloaded = ProfileRegistry(registry.default_store)
    token = asyncio.run(reloaded.store_for(profile_id).get("mcp_auth_token"))
    assert asyncio.run(reloaded.resolve_token(token)) == profile_id
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_unconfigured_required_connection_does_not_spawn_a_child(make_app, monkeypatch):
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
    from mcp_admin_core.process import McpProcessManager

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    set_launch_policy(LaunchPolicy(command="fixed-child", connection_keys=frozenset({"api_key"}),
                                   required_connection=(("api_key", "API Key"),)))
    starts = []

    async def fake_start(self):
        starts.append(self)
        return True

    monkeypatch.setattr(McpProcessManager, "start", fake_start)
    auth = _admin(client)
    created = client.post("/api/profiles", headers=auth, json={"label": "Unconfigured"})
    assert created.status_code == 201
    assert starts == []
    registry = get_profile_registry()
    profile_id = created.json()["id"]
    asyncio.run(registry.start_all())
    assert starts == []
    asyncio.run(registry.store_for(profile_id).patch("connection", {"api_key": "configured"}))
    asyncio.run(registry.start_all())
    assert starts == [registry.manager_for(profile_id)]


def test_profile_index_rejects_corruption_and_duplicate_label(make_app):
    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    assert client.post("/api/profiles", headers=auth, json={"label": "A"}).status_code == 201
    assert client.post("/api/profiles", headers=auth, json={"label": "a"}).status_code == 409
    registry = get_profile_registry()
    asyncio.run(registry.default_store.put("profile_index", {"../unsafe": {"label": "bad", "port": 18000}}))
    assert client.get("/api/profiles", headers=auth).status_code == 503
    assert client.get("/private_any/health").status_code == 503


def test_product_profile_limit_is_enforced_without_invalidating_existing_index(make_app):
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    set_launch_policy(LaunchPolicy(command="fixed-child", max_extra_profiles=2))
    auth = _admin(client)
    for label in ("first", "second"):
        assert client.post("/api/profiles", headers=auth, json={"label": label}).status_code == 201
    assert client.post("/api/profiles", headers=auth, json={"label": "third"}).status_code == 409
    assert len(client.get("/api/profiles", headers=auth).json()["profiles"]) == 3


def test_cgroup_limit_prevents_extra_children_after_memory_reduction(make_app, monkeypatch):
    import mcp_admin_core.profiles as profiles
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    set_launch_policy(LaunchPolicy(command="fixed-child", max_extra_profiles=5,
                                   profile_child_memory_mib=100))
    monkeypatch.setattr(profiles, "_memory_limit_mib", lambda: 512)
    auth = _admin(client)
    first = client.post("/api/profiles", headers=auth, json={"label": "first"}).json()["id"]
    assert client.post("/api/profiles", headers=auth, json={"label": "second"}).status_code == 409

    monkeypatch.setattr(profiles, "_memory_limit_mib", lambda: 1024)
    second = client.post("/api/profiles", headers=auth, json={"label": "second"}).json()["id"]
    registry = get_profile_registry()
    assert asyncio.run(registry.may_start(second)) is True

    monkeypatch.setattr(profiles, "_memory_limit_mib", lambda: 512)
    assert asyncio.run(registry.may_start(first)) is True
    assert asyncio.run(registry.may_start(second)) is False
    with registry.activate(second):
        assert asyncio.run(registry.manager_for(second).start()) is False


def test_profile_token_enforces_its_own_tool_policy(make_app, monkeypatch):
    client = make_app({"mcp_auth_token": "default-secret", "mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    first = client.post("/api/profiles", headers=auth, json={"label": "first"}).json()["id"]
    second = client.post("/api/profiles", headers=auth, json={"label": "second"}).json()["id"]
    registry = get_profile_registry()
    asyncio.run(registry.store_for(first).patch("tools", {"disabled": ["write"]}))
    asyncio.run(registry.store_for(second).patch("tools", {"disabled": ["read"]}))
    first_token = asyncio.run(registry.store_for(first).get("mcp_auth_token"))
    second_token = asyncio.run(registry.store_for(second).get("mcp_auth_token"))
    calls = []

    def upstream(request):
        calls.append((request.url.port, json.loads(request.content)))
        rpc = calls[-1][1]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"],
                                         "result": {"tools": [{"name": "read"}, {"name": "write"}]}})

    import mcp_admin_core.proxy as proxy
    monkeypatch.setattr(proxy, "_get_client", lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    one = client.post(f"/private_{first_token}/mcp", headers={**headers, "X-MCP-Profile": second}, json=listing)
    two = client.post(f"/private_{second_token}/mcp", headers={**headers, "X-MCP-Profile": first}, json=listing)
    assert one.status_code == two.status_code == 200
    assert [t["name"] for t in one.json()["result"]["tools"]] == ["read"]
    assert [t["name"] for t in two.json()["result"]["tools"]] == ["write"]
    assert [port for port, _ in calls] == [18000, 18001]

    denied = client.post(f"/private_{first_token}/mcp", headers=headers,
                         json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "write", "arguments": {}}})
    assert denied.status_code == 200
    assert denied.json()["result"]["isError"] is True
    assert len(calls) == 2


def test_admin_password_is_global_and_profile_index_survives_save(make_app):
    from management_client import revision_headers

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    auth = _admin(client)
    created = client.post("/api/profiles", headers=auth, json={"label": "second"}).json()["id"]
    selected = {**auth, "X-MCP-Profile": created}
    headers = revision_headers(client, selected, read_path="/api/settings")
    assert client.put("/api/settings/admin_password", headers=headers,
                      json={"value": "different-admin-password"}).status_code == 409
    assert client.post("/api/auth/login", json={"password": "admin"}).status_code == 200
    registry = get_profile_registry()
    root = registry.default_store
    snapshot = asyncio.run(root.load())
    snapshot["profile_index"] = {}
    asyncio.run(root.save(snapshot))
    assert created in asyncio.run(root.get("profile_index"))


def test_n8n_internal_child_bearer_is_per_profile(make_app):
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999,
                                     "env": {"AUTH_TOKEN": "root-internal-secret"}},
                       "proxy": {"bearer_token": "root-internal-secret"}})
    set_launch_policy(LaunchPolicy(command="dummy", env_keys=frozenset({"AUTH_TOKEN"})))
    auth = _admin(client)
    ids = [client.post("/api/profiles", headers=auth, json={"label": f"n8n-{i}"}).json()["id"] for i in range(2)]
    cfgs = [asyncio.run(get_profile_registry().store_for(pid).load()) for pid in ids]
    internal = [cfg["mcp_server"]["env"]["AUTH_TOKEN"] for cfg in cfgs]
    assert len(set(internal + ["root-internal-secret"])) == 3
    assert [cfg["proxy"]["bearer_token"] for cfg in cfgs] == internal


def test_registry_reloads_saved_profiles_and_child_env_uses_their_files(make_app, monkeypatch):
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
    import mcp_admin_core.process as process

    client = make_app({"mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}})
    set_launch_policy(LaunchPolicy(command="dummy-child", args=("--port", "{port}"),
                                   connection_keys=frozenset({"username", "password"})))
    auth = _admin(client)
    ids = [client.post("/api/profiles", headers=auth, json={"label": f"person-{i}"}).json()["id"] for i in range(2)]
    registry = get_profile_registry()
    for i, pid in enumerate(ids):
        asyncio.run(registry.store_for(pid).patch("connection", {"username": f"person-{i}", "password": f"key-{i}"}))
    reloaded = ProfileRegistry(registry.default_store)
    captured = []

    class Child:
        returncode = None
        pid = 12345
        stdout = None

        def send_signal(self, sig):
            self.returncode = 0

        async def wait(self):
            return 0

    async def spawn(*cmd, **kwargs):
        captured.append((cmd, kwargs["env"]))
        return Child()

    async def quiet_drain(self, proc):
        return None

    async def quiet_probe(self, proc, snapshot, revision):
        return None

    monkeypatch.setattr(process.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process.McpProcessManager, "_drain_logs", quiet_drain)
    monkeypatch.setattr(process.McpProcessManager, "_verify_configuration", quiet_probe)

    async def scenario():
        await reloaded.start_all()
        await reloaded.stop_all()

    asyncio.run(scenario())
    assert [cmd[-1] for cmd, _ in captured] == ["18000", "18001"]
    assert [env["USERNAME"] for _, env in captured] == ["person-0", "person-1"]
    assert [env["PASSWORD"] for _, env in captured] == ["key-0", "key-1"]
    assert [env["MCP_ADMIN_CONFIG"] for _, env in captured] == [str(reloaded.store_for(pid).path) for pid in ids]
