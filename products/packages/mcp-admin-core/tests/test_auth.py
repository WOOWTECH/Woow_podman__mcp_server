"""AuthMiddleware + login router 回歸測試 —— allow-list、JWT 登入與保護。"""

from __future__ import annotations

from management_client import revision_headers, async_revision_headers

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
import asyncio
import threading
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest

from mcp_admin_core.auth.middleware import JWT_SECRET


def _login(client, password="admin"):
    response = client.post("/api/auth/login", json={"password": password})
    assert response.status_code == 200
    token = response.json()["token"]
    client.cookies.clear()
    return token


def _read_with(client, token, transport):
    # Each transport is tested alone, so a fresh cookie cannot hide a stale JWT.
    client.cookies.clear()
    if transport == "cookie":
        client.cookies.set("mcp-admin-token", token)
        return client.get("/api/settings")
    if transport == "query":
        return client.get("/api/settings", params={"token": token})
    return client.get("/api/settings", headers={"Authorization": f"Bearer {token}"})


def test_healthz_is_public(make_app):
    c = make_app()
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_non_api_path_is_public(make_app):
    c = make_app()
    # 非 /api/ 路徑不應被 middleware 擋（無對應路由 → 404，但不是 401）
    r = c.get("/some-spa-route")
    assert r.status_code != 401


def test_protected_api_requires_token(make_app):
    c = make_app()
    assert c.get("/api/settings").status_code == 401


def test_login_wrong_password_rejected(make_app):
    c = make_app()
    r = c.post("/api/auth/login", json={"password": "wrong"})
    assert r.status_code == 401


def test_login_success_returns_token(make_app):
    c = make_app()  # 預設 admin_password = "admin"
    r = c.post("/api/auth/login", json={"password": "admin"})
    assert r.status_code == 200
    assert "token" in r.json()


def test_default_app_does_not_require_or_issue_session_ids(make_app):
    c = make_app()
    token = _login(c)
    assert c.app.state.admin_session_policy is None
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    assert "jti" not in payload
    for transport in ("bearer", "cookie", "query"):
        assert _read_with(c, token, transport).status_code == 200


def test_bearer_token_grants_access(make_app):
    c = make_app()
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    r = c.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_invalid_token_rejected(make_app):
    c = make_app()
    r = c.get("/api/settings", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


@pytest.mark.parametrize("transport", ["bearer", "cookie", "query"])
def test_deeply_nested_token_header_is_401_not_500(make_app, transport):
    """GHSA-8wjv-2p76-3863：驗簽前解析極深巢狀 JSON header 不得變成 HTTP 500。

    PyJWT 2.15.x 已修；這是縱深防禦回歸測試——不論 PyJWT 版本，剖析階段的
    RecursionError 都必須回 401（且伺服器仍能服務下一個請求）。
    """
    import base64

    c = make_app()
    nested = "[" * 40000  # base64 後約 53KB，低於 httpx URL 元件上限（query 傳送也能測）
    header = base64.urlsafe_b64encode(nested.encode()).decode().rstrip("=")
    token = f"{header}.e30.c2ln"
    c.cookies.clear()
    if transport == "bearer":
        r = c.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
    elif transport == "cookie":
        c.cookies.set("mcp-admin-token", token)
        r = c.get("/api/settings")
    else:
        r = c.get("/api/settings", params={"token": token})
    assert r.status_code in (400, 401, 413, 414, 431), r.status_code
    c.cookies.clear()
    assert c.get("/healthz").status_code == 200


def test_verify_token_swallows_recursion_error(monkeypatch):
    """_verify_token 對任何 RecursionError 回 None（AuthMiddleware 因此回 401）。"""
    from mcp_admin_core.auth import middleware

    def boom(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(middleware.jwt, "decode", boom)
    assert middleware._verify_token("a.b.c") is None


def test_password_change_revokes_existing_bearer_session(make_app):
    """同一行程更新密碼後，舊 JWT 失效，新密碼可重新登入。"""
    c = make_app()
    old_token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    old_headers = {"Authorization": f"Bearer {old_token}"}
    assert c.get("/api/settings", headers=old_headers).status_code == 200

    changed = c.put(
        "/api/settings/admin_password",
        json={"value": "changed-password"},
        headers=revision_headers(c, old_headers, read_path='/api/settings'),
    )
    assert changed.status_code == 200
    assert c.get("/api/settings", headers=old_headers).status_code == 401

    assert c.post("/api/auth/login", json={"password": "admin"}).status_code == 401
    fresh = c.post("/api/auth/login", json={"password": "changed-password"})
    assert fresh.status_code == 200
    assert c.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {fresh.json()['token']}"},
    ).status_code == 200


@pytest.mark.parametrize("transport", ["bearer", "cookie", "query"])
def test_each_transport_revoked_even_after_password_rotated_back(make_app, transport):
    c = make_app()
    original = _login(c)
    assert _read_with(c, original, transport).status_code == 200
    assert c.put(
        "/api/settings/admin_password", json={"value": "changed-password"},
        headers=revision_headers(c, {"Authorization": f"Bearer {original}"}, read_path='/api/settings'),
    ).status_code == 200
    assert _read_with(c, original, transport).status_code == 401

    intermediate = _login(c, "changed-password")
    assert c.put(
        "/api/settings/admin_password", json={"value": "admin"},
        headers=revision_headers(c, {"Authorization": f"Bearer {intermediate}"}, read_path='/api/settings'),
    ).status_code == 200
    assert _read_with(c, original, transport).status_code == 401
    assert _read_with(c, intermediate, transport).status_code == 401
    current = _login(c)
    assert _read_with(c, current, transport).status_code == 200


def test_same_password_write_explicitly_revokes_sessions(make_app):
    c = make_app()
    token = _login(c)
    assert c.put(
        "/api/settings/admin_password", json={"value": "admin"},
        headers=revision_headers(c, {"Authorization": f"Bearer {token}"}, read_path='/api/settings'),
    ).status_code == 200
    assert _read_with(c, token, "bearer").status_code == 401
    assert _read_with(c, _login(c), "bearer").status_code == 200


def test_rejected_password_write_and_unrelated_settings_keep_session(make_app):
    c = make_app({"mcp_auth_token": "local-opaque-token"})
    token = _login(c)
    headers = {"Authorization": f"Bearer {token}"}
    assert c.put("/api/settings/admin_password", json={"value": "x"}, headers=revision_headers(c, headers, read_path='/api/settings')).status_code == 400
    assert c.put("/api/settings/proxy", json={"timeout": 42}, headers=revision_headers(c, headers, read_path='/api/settings')).status_code == 200
    read = _read_with(c, token, "bearer")
    assert read.status_code == 200
    assert read.json()["proxy"]["timeout"] == 42
    assert "*" in read.json()["mcp_auth_token"]
    assert c.post("/api/settings/mcp_auth_token/reveal", headers=headers).json()["value"] == "local-opaque-token"


def test_password_rotation_leaves_opaque_mcp_token_unchanged(make_app):
    c = make_app({"mcp_auth_token": "local-opaque-token"})
    old = _login(c)
    assert c.put(
        "/api/settings/admin_password", json={"value": "changed-password"},
        headers=revision_headers(c, {"Authorization": f"Bearer {old}"}),
    ).status_code == 200
    current = _login(c, "changed-password")
    read = _read_with(c, current, "bearer")
    assert read.status_code == 200
    assert "*" in read.json()["mcp_auth_token"]
    headers = {"Authorization": f"Bearer {current}"}
    assert c.post("/api/settings/mcp_auth_token/reveal", headers=headers).json()["value"] == "local-opaque-token"


def test_mcp_token_rotation_keeps_current_admin_session_and_revoked_session_denied(make_app):
    c = make_app({"mcp_auth_token": "local-opaque-token"})
    old = _login(c)
    assert c.put(
        "/api/settings/admin_password", json={"value": "changed-password"},
        headers=revision_headers(c, {"Authorization": f"Bearer {old}"}),
    ).status_code == 200
    current = _login(c, "changed-password")
    rotated = c.post(
        "/api/settings/mcp_auth_token/rotate",
        headers=revision_headers(c, {"Authorization": f"Bearer {current}"}, read_path='/api/settings'),
    )
    assert rotated.status_code == 200
    read = _read_with(c, current, "bearer")
    assert read.status_code == 200
    assert "*" in read.json()["mcp_auth_token"]
    headers = {"Authorization": f"Bearer {current}"}
    assert c.post("/api/settings/mcp_auth_token/reveal", headers=headers).json()["value"] == rotated.json()["token"]
    assert rotated.json()["token"] != "local-opaque-token"
    assert _read_with(c, old, "bearer").status_code == 401


def test_session_survives_store_reload_with_same_signing_key(make_app, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient
    from mcp_admin_core import create_app

    c = make_app()
    token = _login(c)
    # A new application/store reads the same persisted config, without rewriting it.
    monkeypatch.setattr(store_mod, "_instance", None)
    fresh_client = TestClient(create_app(title="Restarted Test Admin"))
    assert _read_with(fresh_client, token, "bearer").status_code == 200
    assert fresh_client.put(
        "/api/settings/admin_password", json={"value": "changed-password"},
        headers=revision_headers(fresh_client, {"Authorization": f"Bearer {token}"}, read_path='/api/settings'),
    ).status_code == 200
    monkeypatch.setattr(store_mod, "_instance", None)
    reloaded = TestClient(create_app(title="Reloaded Test Admin"))
    assert _read_with(reloaded, token, "bearer").status_code == 401
    assert _read_with(reloaded, _login(reloaded, "changed-password"), "bearer").status_code == 200


def test_persisted_revocation_survives_real_process_restart(tmp_path):
    import mcp_admin_core
    source_file = Path(mcp_admin_core.__file__).resolve()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"admin_password": "local-persist-password"}))
    config.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL") if k in os.environ}
    env.update({
        "MCP_ADMIN_CONFIG": str(config),
        "JWT_SECRET": "local-process-fixture-signing-key-at-least32",
        # 避免借用的 venv editable install 指回另一工作樹；子程序必須測同一份 source。
        "PYTHONPATH": str(source_file.parent.parent),
    })
    code = (
        "import uvicorn, mcp_admin_core; from pathlib import Path; "
        f"assert Path(mcp_admin_core.__file__).resolve() == Path({str(source_file)!r}); "
        "from mcp_admin_core import create_app; "
        f"uvicorn.run(create_app(title='Local restart fixture'), host='127.0.0.1', port={port}, "
        "log_level='warning', access_log=False)"
    )

    @contextmanager
    def server():
        proc = subprocess.Popen(
            [sys.executable, "-c", code], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                deadline = time.monotonic() + 10
                while True:
                    assert proc.poll() is None, "Local restart fixture exited before readiness"
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, "Local restart fixture readiness timeout"
                    time.sleep(0.05)
                yield client
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    with server() as c:
        original = _login(c, "local-persist-password")
        assert _read_with(c, original, "bearer").status_code == 200
    with server() as c:
        assert _read_with(c, original, "bearer").status_code == 200
        assert c.put(
            "/api/settings/admin_password", json={"value": "local-changed-password"},
            headers=revision_headers(c, {"Authorization": f"Bearer {original}"}, read_path='/api/settings'),
        ).status_code == 200
        assert _read_with(c, original, "bearer").status_code == 401
        current = _login(c, "local-changed-password")
    with server() as c:
        assert _read_with(c, original, "bearer").status_code == 401
        assert _read_with(c, current, "bearer").status_code == 200


def test_failed_password_persistence_does_not_change_auth(make_app, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient

    c = make_app()
    token = _login(c)
    store = store_mod.get_config_store()
    persisted_before = store.path.read_bytes()

    def disk_failure(data):
        raise OSError("local fixture disk failure")

    with monkeypatch.context() as m:
        m.setattr(store, "_write_sync", disk_failure)
        failure_client = TestClient(c.app, raise_server_exceptions=False)
        assert failure_client.put(
            "/api/settings/admin_password", json={"value": "changed-password"},
            headers=revision_headers(failure_client, {"Authorization": f"Bearer {token}"}, read_path='/api/settings'),
        ).status_code == 500
    assert store.path.read_bytes() == persisted_before
    assert _read_with(c, token, "bearer").status_code == 200
    assert c.post("/api/auth/login", json={"password": "changed-password"}).status_code == 401
    assert _read_with(c, _login(c), "bearer").status_code == 200


@pytest.mark.parametrize("generation", [None, "", 42, ["invalid"]])
def test_legacy_or_invalid_generation_is_rejected(make_app, generation):
    c = make_app()
    _login(c)
    # Only this migration negative fixture signs a local old-format JWT.
    payload = {"sub": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    if generation is not None:
        payload["session_generation"] = generation
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    assert _read_with(c, token, "bearer").status_code == 401


async def test_cancelled_unrelated_write_cannot_revive_revoked_session_on_store_restart(
    store_env, monkeypatch,
):
    """正常登入／改密碼後，取消舊 settings writer 仍不可在重載時復活 JWT。"""
    from mcp_admin_core import create_app
    from mcp_admin_core.config.store import ConfigStore
    import mcp_admin_core.config.store as store_mod

    app = create_app(title="Cancellation Regression")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"password": "admin"})
        assert login.status_code == 200
        old_token = login.json()["token"]
        client.cookies.clear()
        headers = {"Authorization": f"Bearer {old_token}"}
        started, release = threading.Event(), threading.Event()
        write = store_env._write_sync

        def held_write(data):
            if data["admin_password"] == "admin" and data["proxy"]["timeout"] == 42:
                started.set()
                if not release.wait(5):
                    raise TimeoutError("controlled writer was not released")
            write(data)

        monkeypatch.setattr(store_env, "_write_sync", held_write)
        cancelled = asyncio.create_task(store_env.patch("proxy", {"timeout": 42}))
        rotation = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            cancelled.cancel()
            await asyncio.sleep(0.02)
            async def rotate_after_explicit_read():
                # Read after the cancelled durable save finishes, then submit the
                # visible revision. Do not silently rebase a previously sent write.
                condition = await async_revision_headers(client, headers)
                return await client.put("/api/settings/admin_password",
                    json={"value": "changed-password"}, headers=condition)
            rotation = asyncio.create_task(rotate_after_explicit_read())
            await asyncio.sleep(0.04)
        finally:
            release.set()
            outcomes = await asyncio.gather(
                cancelled, *([rotation] if rotation is not None else []), return_exceptions=True,
            )
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert outcomes[1].status_code == 200
        assert (await client.get("/api/settings", headers=headers)).status_code == 401
        current_login = await client.post("/api/auth/login", json={"password": "changed-password"})
        assert current_login.status_code == 200
        current_token = current_login.json()["token"]

    # A new application/store reads persisted auth with the same test signing key.
    monkeypatch.setattr(store_mod, "_instance", ConfigStore(path=store_env.path))
    restarted = create_app(title="Cancellation Restart Regression")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://test") as client:
        assert (await client.get("/api/settings", headers=headers)).status_code == 401
        assert (await client.get("/api/settings", headers={"Authorization": f"Bearer {current_token}"})).status_code == 200
