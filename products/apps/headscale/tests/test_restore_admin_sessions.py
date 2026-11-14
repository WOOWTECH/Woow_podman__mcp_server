"""In-place restore of a Headscale config volume and its admin sessions database (2026-11-13 mp-gap-prod-restore P1).

The admin process creates ``admin-sessions.sqlite3`` next to ``config.json`` on every start, so every backup of a
Headscale volume holds one. ``python -m mcp_admin_core.restore_inplace`` (runbook step 5) must restore such a backup
and must not bring the database back: the restored service starts with a new, empty database (new random epoch), so
no admin login from before the restore is accepted again — not one issued before the backup, not one logged out
after it, not one issued after it — even if its signature and session generation were still valid. New logins and
per-login logout keep working on the new database.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import jwt
import pytest

from mcp_admin_core.auth import middleware as auth_module
from mcp_admin_core.config import store as store_module
from mcp_admin_core.operations import OperationJournal
from headscale_mcp_admin.admin_sessions import AdminSessions
from headscale_mcp_admin.main import app

PASSWORD = "restore-fixture-password"
NEW_PASSWORD = "restore-fixture-password-after-reset"
SESSIONS = "admin-sessions.sqlite3"
PROFILES = {"c7666c71747e64962b7eefa9": 18000, "1d7ae1b9b680b0e4e5455fee": 18001}

# restore_inplace 設計上拒絕以 root 執行（服務以 uid 1000 執行、只讀自己擁有的檔案）；CI 以 uid 1000 真的跑。
executes_restore = pytest.mark.skipif(os.geteuid() == 0, reason="restore_inplace 設計上拒絕 root；CI 以 uid 1000 執行")


def write_volume(root: Path) -> list[str]:
    """A Headscale volume (default profile + two profiles, journals) in the shape of the production one."""
    root.mkdir()
    index = {pid: {"label": f"acct-{i}", "port": port} for i, (pid, port) in enumerate(PROFILES.items())}
    tokens = []
    for i, (directory, port, profile_index) in enumerate(
            [(root, 8000, index)] + [(root / "profiles" / pid, port, {}) for pid, port in PROFILES.items()]):
        directory.mkdir(parents=True, exist_ok=True)
        token = f"restore-fixture-mcp-token-{i}-" + "x" * 24
        tokens.append(token)
        config = {"admin_password": PASSWORD if i == 0 else f"restore-fixture-profile-password-{i}",
                  "admin_session_generation": f"restore-fixture-session-{i}", "admin_session_revocations": {},
                  "mcp_auth_token": token, "mcp_auth_revoked": False,
                  "mcp_auth_generation": f"restore-fixture-generation-{i}",
                  "token_history": [{"action": "auto_issue", "at": "2026-11-13T04:56:36Z"}],
                  "connection": {"base_url": "", "api_key": ""}, "proxy": {"timeout": 86400},
                  "mcp_server": {"command": "headscale-mcp-server", "args": [], "port": port, "env": {},
                                 "env_prefix": "HEADSCALE_MCP_"},
                  "tools": {"disabled": [], "disabled_operations": {},
                            "owner_defaults_20261025": {"applied": "fresh_install", "source": "existing_config"}},
                  "profile_index": profile_index}
        path = directory / "config.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        OperationJournal(path)._write({"version": 1, "key": "c" * 64, "expired_before_ms": 0, "receipts": {}})
    return tokens


def credential(token, transport):
    if transport == "cookie":
        return {"headers": {"Cookie": f"mcp-admin-token={token}"}}
    if transport == "query":
        return {"params": {"token": token}}
    return {"headers": {"Authorization": f"Bearer {token}"}}


async def login(client, password):
    response = await client.post("/api/auth/login", json={"password": password})
    client.cookies.clear()
    return response.status_code, (response.json().get("token") if response.status_code == 200 else None)


def start_service(monkeypatch, live: Path) -> store_module.ConfigStore:
    """A fresh admin process on *live*: new config store and admin session policy (no state from before)."""
    store = store_module.ConfigStore(live / "config.json")
    monkeypatch.setattr(store_module, "_instance", store)
    monkeypatch.setitem(app.state._state, "admin_session_policy", AdminSessions())
    return store


def resign(token: str, **changes) -> str:
    payload = jwt.decode(token, auth_module.JWT_SECRET, algorithms=["HS256"])
    payload.update(changes)
    return jwt.encode(payload, auth_module.JWT_SECRET, algorithm="HS256")


@executes_restore
async def test_restore_drops_admin_sessions_and_no_earlier_admin_login_comes_back(tmp_path, monkeypatch):
    live, backup = tmp_path / "live", tmp_path / "backup"
    tokens = write_volume(live)
    start_service(monkeypatch, live)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin.example") as client:
        # Before the backup: one login kept, one logged out (in the backup's database), one logged out later.
        logins = {}
        for name in ("kept", "logged_out_before_backup", "logged_out_after_backup"):
            status, logins[name] = await login(client, PASSWORD)
            assert status == 200
        auth = credential(logins["logged_out_before_backup"], "bearer")
        assert (await client.post("/api/auth/logout", **auth)).status_code == 200
        assert (live / SESSIONS).is_file()

        # The backup (stop-write copy of the whole volume, as the runbook's restore source would hold it).
        shutil.copytree(live, backup)
        assert (backup / SESSIONS).is_file()

        # After the backup: one more logout and a new login. Both are only in the live database.
        auth = credential(logins["logged_out_after_backup"], "bearer")
        assert (await client.post("/api/auth/logout", **auth)).status_code == 200
        status, logins["issued_after_backup"] = await login(client, PASSWORD)
        assert status == 200
        for name, token in logins.items():
            expected = 200 if name in ("kept", "issued_after_backup") else 401
            assert (await client.get("/api/headscale/connection", **credential(token, "bearer"))).status_code == expected

        # Runbook step 5 (service stopped): the in-place restore from the backup.
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-m", "mcp_admin_core.restore_inplace", str(backup), str(live)],
                                env=env, capture_output=True, text=True, timeout=120, check=False)
        assert result.returncode == 0, result.stderr
        assert not any(name.startswith(SESSIONS) for name in os.listdir(live))  # not restored, not kept

        # Runbook step 7: the service starts on the restored volume and creates a new, empty database.
        store = start_service(monkeypatch, live)
        await app.state.admin_session_policy.initialize()
        assert (live / SESSIONS).stat().st_mode & 0o777 == 0o600
        assert (await login(client, PASSWORD))[0] == 401  # the restore replaced the admin password
        for token in tokens:  # every MCP token of the backup (all profiles) is refused by the product gateway
            response = await client.post(f"/private_{token}/mcp",
                                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 403
        # Runbook step 8: the platform resets the admin password; then a new login works.
        await store.put("admin_password", NEW_PASSWORD)
        generation = await store.get("admin_session_generation")
        status, fresh = await login(client, NEW_PASSWORD)
        assert status == 200
        assert (await client.get("/api/headscale/connection", **credential(fresh, "bearer"))).status_code == 200

        for name, token in logins.items():
            for transport in ("cookie", "bearer", "query"):
                response = await client.get("/api/headscale/connection", **credential(token, transport))
                assert response.status_code == 401, (name, transport)
            # Even carrying today's session generation, a login from before the restore does not come back: its
            # session id belongs to the database that was not restored.
            forged = resign(token, session_generation=generation)
            assert (await client.get("/api/headscale/connection", **credential(forged, "bearer"))).status_code == 401
        # Control: the same re-signing keeps a login of the new database valid.
        assert (await client.get("/api/headscale/connection",
                                 **credential(resign(fresh, session_generation=generation), "bearer"))).status_code == 200

        # Per-login logout works on the new database; other new logins stay valid.
        status, other = await login(client, NEW_PASSWORD)
        assert status == 200
        assert (await client.post("/api/auth/logout", **credential(fresh, "bearer"))).status_code == 200
        assert (await client.get("/api/headscale/connection", **credential(fresh, "bearer"))).status_code == 401
        assert (await client.get("/api/headscale/connection", **credential(other, "bearer"))).status_code == 200
