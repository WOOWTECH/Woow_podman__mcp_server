"""Session revocation through the product HTTP authentication boundary."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import jwt
import pytest
from starlette.responses import StreamingResponse

from mcp_admin_core.config import store as store_module
from mcp_admin_core.auth import middleware as auth_module
from headscale_mcp_admin.admin_sessions import AdminSessions
from headscale_mcp_admin.main import app

PASSWORD = "session-fixture-password"


@pytest.fixture
async def admin_client(tmp_path, monkeypatch):
    store = store_module.ConfigStore(tmp_path / "config.json")
    await store.put("admin_password", PASSWORD)
    monkeypatch.setattr(store_module, "_instance", store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://admin.example") as client:
        yield client, store


async def login(client):
    response = await client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    client.cookies.clear()
    return response.json()["token"]


def credential(token, transport):
    if transport == "cookie":
        return {"headers": {"Cookie": f"mcp-admin-token={token}"}}
    if transport == "query":
        return {"params": {"token": token}}
    return {"headers": {"Authorization": f"Bearer {token}"}}


async def test_logout_revokes_copied_cookie(admin_client):
    client, _ = admin_client
    token = await login(client)
    auth = credential(token, "cookie")
    assert (await client.get("/api/headscale/connection", **auth)).status_code == 200
    assert (await client.post("/api/auth/logout", **auth)).status_code == 200
    assert (await client.get("/api/headscale/connection", **auth)).status_code == 401


@pytest.mark.parametrize("logout_transport", ["cookie", "bearer", "query"])
@pytest.mark.parametrize("replay_transport", ["cookie", "bearer", "query"])
async def test_logout_only_revokes_current_login_across_all_entries(admin_client, logout_transport, replay_transport):
    client, store = admin_client
    first, second = await login(client), await login(client)
    generation = await store.get("admin_session_generation")
    for _ in range(2):
        assert (await client.post("/api/auth/logout", **credential(first, logout_transport))).status_code == 200
    revoked = credential(first, replay_transport)
    for method, path in [("GET", "/api/headscale/connection"), ("GET", "/api/headscale/catalog"),
                         ("GET", "/api/tokens"), ("GET", "/api/logs/stream"),
                         ("POST", "/api/headscale/tools/headscale_users"),
                         ("PUT", "/api/headscale/connection")]:
        assert (await client.request(method, path, **revoked)).status_code == 401
    assert (await client.get("/api/headscale/connection", **credential(second, replay_transport))).status_code == 200
    assert await store.get("admin_session_generation") == generation


async def test_logins_in_same_second_have_independent_random_ids(admin_client, monkeypatch):
    client, _ = admin_client
    fixed = datetime.now(timezone.utc)

    class FrozenDateTime:
        @staticmethod
        def now(tz):
            return fixed

    monkeypatch.setattr(auth_module, "datetime", FrozenDateTime)
    first, second = await login(client), await login(client)
    claims = [jwt.decode(token, auth_module.JWT_SECRET, algorithms=["HS256"]) for token in (first, second)]
    assert claims[0]["iat"] == claims[1]["iat"]
    assert claims[0]["jti"] != claims[1]["jti"]
    assert len(claims[0]["jti"]) >= 32


async def test_revocation_survives_policy_and_config_reload(admin_client, monkeypatch):
    client, store = admin_client
    first, second = await login(client), await login(client)
    assert (await client.post("/api/auth/logout", **credential(first, "bearer"))).status_code == 200
    monkeypatch.setattr(store_module, "_instance", store_module.ConfigStore(store.path))
    # Starlette State stores attributes in _state; patching the proxy object
    # can delete this policy on teardown instead of restoring its old value.
    monkeypatch.setitem(app.state._state, "admin_session_policy", AdminSessions())
    await app.state.admin_session_policy.initialize()
    assert (await client.get("/api/headscale/connection", **credential(first, "bearer"))).status_code == 401
    assert (await client.get("/api/headscale/connection", **credential(second, "bearer"))).status_code == 200
    await store_module.get_config_store().put("admin_password", "changed-fixture-password")
    assert (await client.get("/api/headscale/connection", **credential(second, "bearer"))).status_code == 401


@pytest.mark.parametrize("jti", [None, "", 12, [], "wrong", "x" * 32 + "." + "y" * 43])
async def test_legacy_and_malformed_sessions_rejected_without_logout_records(admin_client, jti):
    client, store = admin_client
    original = await login(client)
    payload = jwt.decode(original, auth_module.JWT_SECRET, algorithms=["HS256"])
    if jti is None:
        del payload["jti"]
    else:
        payload["jti"] = jti
    token = jwt.encode(payload, auth_module.JWT_SECRET, algorithm="HS256")
    for transport in ("cookie", "bearer", "query"):
        assert (await client.get("/api/headscale/connection", **credential(token, transport))).status_code == 401
        assert (await client.post("/api/auth/logout", **credential(token, transport))).status_code == 401
    with sqlite3.connect(store.path.with_name("admin-sessions.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM revoked_sessions").fetchone()[0] == 0


async def test_logout_uses_same_header_cookie_query_precedence(admin_client):
    client, _ = admin_client
    first, second = await login(client), await login(client)
    both = {"Authorization": "Bearer " + first, "Cookie": "mcp-admin-token=" + second}
    assert (await client.post("/api/auth/logout", headers=both)).status_code == 200
    assert (await client.get("/api/headscale/connection", **credential(first, "bearer"))).status_code == 401
    assert (await client.get("/api/headscale/connection", **credential(second, "bearer"))).status_code == 200
    both["Authorization"] = "Bearer invalid"
    assert (await client.post("/api/auth/logout", headers=both)).status_code == 401
    assert (await client.get("/api/headscale/connection", **credential(second, "bearer"))).status_code == 200


async def test_parallel_logouts_persist_hashes_and_original_expiry_only(admin_client):
    client, store = admin_client
    tokens = [await login(client) for _ in range(5)]
    replies = await asyncio.gather(*(client.post("/api/auth/logout", **credential(token, "bearer"))
                                     for token in tokens * 2))
    assert all(response.status_code == 200 for response in replies)
    path = store.path.with_name("admin-sessions.sqlite3")
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT jti_hash, expires_at FROM revoked_sessions").fetchall()
    assert len(rows) == 5
    for token in tokens:
        claims = jwt.decode(token, auth_module.JWT_SECRET, algorithms=["HS256"])
        assert (hashlib.sha256(claims["jti"].encode()).hexdigest(), claims["exp"]) in rows
        assert token.encode() not in path.read_bytes()
        assert claims["jti"].encode() not in path.read_bytes()
    assert PASSWORD.encode() not in path.read_bytes()


async def test_unavailable_database_blocks_reads_login_and_logout_then_recovers(admin_client):
    client, store = admin_client
    token = await login(client)
    path = store.path.with_name("admin-sessions.sqlite3")
    backup = path.with_suffix(".backup")
    path.rename(backup)
    for method, route in [("GET", "/api/headscale/connection"), ("POST", "/api/auth/logout")]:
        response = await client.request(method, route, **credential(token, "bearer"))
        assert response.status_code == 503
        assert response.json()["detail"] == "ADMIN_SESSION_STORE_UNAVAILABLE"
        assert "set-cookie" not in response.headers
    assert (await client.post("/api/auth/login", json={"password": PASSWORD})).status_code == 503
    assert not path.exists()
    backup.rename(path)
    assert (await client.get("/api/headscale/connection", **credential(token, "bearer"))).status_code == 200
    assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 200
    assert (await client.get("/api/headscale/connection", **credential(token, "bearer"))).status_code == 401


async def test_locked_writer_does_not_report_success_or_clear_cookie(admin_client):
    client, store = admin_client
    token = await login(client)
    db = sqlite3.connect(store.path.with_name("admin-sessions.sqlite3"))
    try:
        db.execute("BEGIN IMMEDIATE")
        response = await client.post("/api/auth/logout", **credential(token, "bearer"))
        assert response.status_code == 503
        assert "set-cookie" not in response.headers
    finally:
        db.rollback()
        db.close()
    assert (await client.get("/api/headscale/connection", **credential(token, "bearer"))).status_code == 200
    assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 200


async def test_recreated_database_cannot_resurrect_existing_tokens(admin_client, monkeypatch):
    client, store = admin_client
    token = await login(client)
    assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 200
    store.path.with_name("admin-sessions.sqlite3").unlink()
    monkeypatch.setitem(app.state._state, "admin_session_policy", AdminSessions())
    await app.state.admin_session_policy.initialize()
    assert (await client.get("/api/headscale/connection", **credential(token, "bearer"))).status_code == 401
    assert (await client.get("/api/headscale/connection", **credential(await login(client), "bearer"))).status_code == 200


async def test_expired_tokens_and_bad_signatures_do_not_create_revocations(admin_client):
    client, store = admin_client
    token = await login(client)
    payload = jwt.decode(token, auth_module.JWT_SECRET, algorithms=["HS256"])
    payload["exp"] = 1
    expired = jwt.encode(payload, auth_module.JWT_SECRET, algorithm="HS256")
    for invalid in (expired, "invalid", ""):
        assert (await client.post("/api/auth/logout", **credential(invalid, "bearer"))).status_code == 401
    with sqlite3.connect(store.path.with_name("admin-sessions.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM revoked_sessions").fetchone()[0] == 0


async def test_pruning_removes_only_expired_revocations(admin_client, monkeypatch):
    from headscale_mcp_admin import admin_sessions
    client, store = admin_client
    token = await login(client)
    assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 200
    expiry = jwt.decode(token, auth_module.JWT_SECRET, algorithms=["HS256"])["exp"]
    await app.state.admin_session_policy.prune()
    assert (await client.get("/api/headscale/connection", **credential(token, "bearer"))).status_code == 401
    monkeypatch.setattr(admin_sessions.time, "time", lambda: expiry + 1)
    await app.state.admin_session_policy.prune()
    with sqlite3.connect(store.path.with_name("admin-sessions.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM revoked_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["logout", "password", "unavailable", "expiry"])
@pytest.mark.parametrize("backpressure", [False, True])
@pytest.mark.parametrize("root_path", ["", "/gateway"])
async def test_auth_change_closes_already_open_admin_stream(admin_client, monkeypatch, change, backpressure, root_path):
    client, store = admin_client
    token = await login(client)
    started, closed = asyncio.Event(), asyncio.Event()
    never = asyncio.Event()

    async def stream():
        async def events():
            try:
                yield b"data: before\n\n"
                await never.wait()
                yield b"data: after\n\n"
            finally:
                closed.set()
        return StreamingResponse(events(), media_type="text/event-stream")

    monkeypatch.setattr(app.router, "routes", list(app.router.routes))
    app.add_api_route("/api/session-test/stream", stream)
    app.router.routes.insert(0, app.router.routes.pop())
    sent = []

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            started.set()
            if backpressure:
                await never.wait()
        sent.append(message)

    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "https", "path": root_path + "/api/session-test/stream",
             "query_string": ("token=" + token).encode(), "root_path": root_path,
             "headers": [(b"host", b"admin.example")], "server": ("admin.example", 443),
             "client": ("127.0.0.1", 12345)}
    running = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert sent[0]["status"] == 200, sent
        if change == "logout":
            assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 200
        elif change == "password":
            await store.put("admin_password", "changed-fixture-password")
        elif change == "unavailable":
            store.path.with_name("admin-sessions.sqlite3").unlink()
        else:
            from headscale_mcp_admin import admin_sessions
            expiry = jwt.decode(token, auth_module.JWT_SECRET, algorithms=["HS256"])["exp"]
            monkeypatch.setattr(admin_sessions.time, "time", lambda: expiry + 1)
        await asyncio.wait_for(asyncio.shield(running), 2)
        assert closed.is_set()
        assert b"data: after" not in b"".join(message.get("body", b"") for message in sent)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_corrupt_database_fails_closed_without_overwriting(admin_client, monkeypatch):
    client, store = admin_client
    token = await login(client)
    path = store.path.with_name("admin-sessions.sqlite3")
    path.write_bytes(b"corrupt session database")
    monkeypatch.setitem(app.state._state, "admin_session_policy", AdminSessions())
    for route in ("/api/headscale/connection", "/api/headscale/catalog"):
        assert (await client.get(route, **credential(token, "bearer"))).status_code == 503
    assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 503
    assert (await client.post("/api/auth/login", json={"password": PASSWORD})).status_code == 503
    assert path.read_bytes() == b"corrupt session database"


def test_revoked_session_survives_real_process_restart(tmp_path):
    """Two independent interpreters share only config, SQLite and a fixed test key."""
    import headscale_mcp_admin.main as main
    import mcp_admin_core

    source = Path(main.__file__).resolve()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"admin_password": PASSWORD}))
    config.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL") if key in os.environ}
    env.update({"MCP_ADMIN_CONFIG": str(config), "JWT_SECRET": "fixture-session-key-at-least-32-characters",
                "HEADSCALE_MCP_SECURE_COOKIE": "false",
                "PYTHONPATH": os.pathsep.join([str(source.parents[1]), str(Path(mcp_admin_core.__file__).resolve().parents[1])])})
    code = (
        "import uvicorn; from pathlib import Path; import headscale_mcp_admin.main as main; "
        f"assert Path(main.__file__).resolve() == Path({str(source)!r}); "
        f"uvicorn.run(main.app, host='127.0.0.1', port={port}, lifespan='off', "
        "log_level='error', access_log=False)"
    )

    @contextmanager
    def server():
        proc = subprocess.Popen([sys.executable, "-c", code], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                deadline = time.monotonic() + 10
                while True:
                    assert proc.poll() is None, "Restart fixture exited before readiness"
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, "Restart fixture readiness timeout"
                    time.sleep(0.05)
                yield client
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    with server() as client:
        tokens = []
        for _ in range(2):
            response = client.post("/api/auth/login", json={"password": PASSWORD})
            assert response.status_code == 200
            tokens.append(response.json()["token"])
            client.cookies.clear()
        assert client.post("/api/auth/logout", **credential(tokens[0], "bearer")).status_code == 200
    with server() as client:
        for transport in ("cookie", "bearer", "query"):
            assert client.get("/api/headscale/connection", **credential(tokens[0], transport)).status_code == 401
            assert client.get("/api/headscale/connection", **credential(tokens[1], transport)).status_code == 200


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/api/tokens/rotate", {}),
    ("PUT", "/api/headscale/connection", {"base_url": "https://queued.example", "apply": False}),
])
@pytest.mark.parametrize("transport", ["bearer", "cookie", "query"])
@pytest.mark.parametrize("change,expected", [("logout", 401), ("unavailable", 503)])
async def test_queued_mutation_rechecks_session_before_journal_or_save(
        admin_client, monkeypatch, method, path, payload, transport, change, expected):
    from mcp_admin_core.operations import OperationJournal

    client, store = admin_client
    token = await login(client)
    auth = credential(token, transport)
    revision = (await client.get("/api/headscale/connection", **auth)).headers["etag"]
    auth.setdefault("headers", {}).update({"If-Match": revision,
        "Idempotency-Key": f"{int(time.time() * 1000)}.queued-session-fixture"})
    queued = asyncio.Event()
    original = store.transition

    @asynccontextmanager
    async def observed_transition():
        queued.set()  # The HTTP request has passed authentication and is now waiting.
        async with original():
            yield

    monkeypatch.setattr(store, "transition", observed_transition)
    running = None
    try:
        async with original():
            running = asyncio.create_task(client.request(method, path, **auth, json=payload))
            await asyncio.wait_for(queued.wait(), 2)
            if change == "logout":
                assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 200
            else:
                store.path.with_name("admin-sessions.sqlite3").unlink()
            before = store.path.read_bytes()
        response = await asyncio.wait_for(running, 2)
        assert response.status_code == expected
        assert "configuration" not in response.text and "operation" not in response.json()
        assert store.path.read_bytes() == before
        assert not OperationJournal(store.path).path.exists()
    finally:
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("transport", ["bearer", "cookie", "query"])
@pytest.mark.parametrize("change,expected", [("logout", 401), ("password", 401), ("unavailable", 503)])
async def test_queued_receipt_read_rechecks_session_before_disclosure(
        admin_client, monkeypatch, transport, change, expected):
    client, store = admin_client
    token, other = await login(client), await login(client)
    headers = credential(token, "bearer")["headers"]
    headers["If-Match"] = (await client.get("/api/headscale/connection", headers=headers)).headers["etag"]
    operation_id = f"{int(time.time() * 1000)}.queued-receipt-fixture"
    headers["Idempotency-Key"] = operation_id
    assert (await client.post("/api/tokens/rotate", headers=headers, json={})).status_code == 200
    path = "/api/operations/" + operation_id
    queued = asyncio.Event()
    original = store.transition

    @asynccontextmanager
    async def observed_transition():
        queued.set()  # Authentication completed; receipt lookup is waiting for the lock.
        async with original():
            yield

    monkeypatch.setattr(store, "transition", observed_transition)
    running = None
    try:
        async with original():
            running = asyncio.create_task(client.get(path, **credential(token, transport)))
            await asyncio.wait_for(queued.wait(), 2)
            if change == "logout":
                assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 200
            elif change == "password":
                await store.put("admin_password", "changed-fixture-password")
            else:
                store.path.with_name("admin-sessions.sqlite3").unlink()
        response = await asyncio.wait_for(running, 2)
        assert response.status_code == expected
        assert "operation" not in response.json() and "result" not in response.json()
        if change == "logout":
            valid = await client.get(path, **credential(other, transport))
            assert valid.status_code == 200
            assert valid.json()["operation"]["state"] == "completed"
    finally:
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_already_accepted_rotation_finishes_after_logout(admin_client, monkeypatch):
    from mcp_admin_core.operations import OperationJournal

    client, store = admin_client
    token = await login(client)
    headers = credential(token, "bearer")["headers"]
    headers["If-Match"] = (await client.get("/api/headscale/connection", headers=headers)).headers["etag"]
    headers["Idempotency-Key"] = f"{int(time.time() * 1000)}.accepted-session-fixture"
    accepted, release = asyncio.Event(), asyncio.Event()
    original = store.change_mcp_token

    async def held_write(*args, **kwargs):
        accepted.set()  # The real route has accepted and journaled this operation.
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(store, "change_mcp_token", held_write)
    running = asyncio.create_task(client.post("/api/tokens/rotate", headers=headers, json={}))
    try:
        await asyncio.wait_for(accepted.wait(), 2)
        assert OperationJournal(store.path).path.exists()
        assert (await client.post("/api/auth/logout", **credential(token, "bearer"))).status_code == 200
        release.set()
        response = await asyncio.wait_for(running, 2)
        assert response.status_code == 200
        assert response.json()["operation"]["state"] == "completed"
        assert await store.get("mcp_auth_token") == response.json()["token"]
        assert (await client.get("/api/headscale/connection", **credential(token, "bearer"))).status_code == 401
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
