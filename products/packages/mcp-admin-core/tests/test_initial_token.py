"""從來沒有過 MCP token 的設定第一次啟動自動發一個（第三輪 G4）。

正式站與第一輪都證實：PaaS 新開的 MCP 服務 ``mcp_auth_token`` 是空的，管理員不先到 Tokens 頁輪替就連不上（266 開通
11 天沒人能用）。現在 core lifespan 在 owner_defaults 之後發第一個 token：與管理頁輪替同一個產生器、同一套
token_history／generation 記帳；撤銷過的不會自動復活；token 不進任何 log。
"""

from __future__ import annotations

from management_client import revision_headers

import json
import logging
import re
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_admin_core.config.store import ConfigStore, never_had_mcp_token

HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.mark.parametrize("cfg, expected", [
    ({}, True),
    ({"mcp_auth_token": "", "mcp_auth_revoked": False, "mcp_auth_generation": "", "token_history": []}, True),
    ({"mcp_auth_token": "   "}, True),
    ({"mcp_auth_token": "an-active-token-0123456789"}, False),
    ({"mcp_auth_token": "", "mcp_auth_revoked": True}, False),  # 撤銷過：不會自動復活
    # 閘道把任何真值都當成已撤銷（mcp_access.AccessLease），這裡也一樣（審查第三輪）
    ({"mcp_auth_token": "", "mcp_auth_revoked": "true"}, False),
    ({"mcp_auth_token": "", "mcp_auth_revoked": 1}, False),
    ({"mcp_auth_token": "", "mcp_auth_revoked": 0}, True),
    ({"mcp_auth_token": "", "mcp_auth_revoked": None}, True),
    ({"mcp_auth_token": "", "mcp_auth_generation": "g"}, False),  # 發過、換過或撤銷過（每次都換 generation）
    ({"mcp_auth_token": "", "token_history": [{"action": "revoke", "rotated_at": "t", "previous_token_masked": "ab***cd"}]},
     False),
    ({"mcp_auth_token": "", "token_history": [{"rotated_at": "t", "previous_token_masked": "ab***cd"}]}, False),  # 舊版紀錄
])
def test_never_had_mcp_token(cfg, expected):
    assert never_had_mcp_token(cfg) is expected


async def test_fresh_config_gets_one_token_with_the_rotation_bookkeeping(tmp_config_path):
    from mcp_admin_core.initial_token import ensure_initial_mcp_token

    store = ConfigStore(path=tmp_config_path)  # 本行程新建的 config.json（全新安裝）
    assert await ensure_initial_mcp_token(store) is True
    cfg = json.loads(tmp_config_path.read_text())
    assert HEX64.match(cfg["mcp_auth_token"]) and cfg["mcp_auth_revoked"] is False
    assert cfg["mcp_auth_generation"]
    assert [(e["action"], e["previous_token_masked"]) for e in cfg["token_history"]] == [("auto_issue", "")]
    assert tmp_config_path.stat().st_mode & 0o777 == 0o600
    before = tmp_config_path.read_bytes()
    assert await ensure_initial_mcp_token(store) is False  # 第二次啟動：已經有 token
    assert await ensure_initial_mcp_token(ConfigStore(path=tmp_config_path)) is False
    assert tmp_config_path.read_bytes() == before


@pytest.mark.parametrize("state", [
    {"mcp_auth_token": "", "mcp_auth_revoked": True, "mcp_auth_generation": "revoked-generation",
     "token_history": [{"action": "revoke", "rotated_at": "t", "previous_token_masked": "ab***cd"}]},
    {"mcp_auth_token": "a-token-the-admin-set-0123456789"},
    {"mcp_auth_token": "", "mcp_auth_generation": "legacy-generation"},
])
async def test_revoked_or_used_configs_are_left_alone(tmp_config_path, state):
    tmp_config_path.write_text(json.dumps({"admin_password": "x", **state}))
    before = tmp_config_path.read_bytes()
    made = []

    def make_token():
        made.append(1)
        return "should-not-be-used"

    assert await ConfigStore(path=tmp_config_path).issue_initial_mcp_token(make_token) is False
    assert made == [] and tmp_config_path.read_bytes() == before


@pytest.mark.parametrize("value", ["true", 1, "false", 0, None])
async def test_store_rejects_a_non_boolean_revocation_flag(tmp_config_path, value):
    """非布林的撤銷旗標整份設定讀不到（fail safe）：自動發 token 不會在這種設定上跑，更不會把旗標改回 False。"""
    from mcp_admin_core.config.store import ConfigUnavailable
    from mcp_admin_core.initial_token import ensure_initial_mcp_token

    tmp_config_path.write_text(json.dumps({"admin_password": "x", "mcp_auth_token": "", "mcp_auth_revoked": value}))
    before = tmp_config_path.read_bytes()
    with pytest.raises(ConfigUnavailable):
        await ensure_initial_mcp_token(ConfigStore(path=tmp_config_path))
    assert tmp_config_path.read_bytes() == before


def test_same_generator_as_rotation():
    from mcp_admin_core.routers import tokens

    assert HEX64.match(tokens.new_token()) and len(tokens.new_token(33)) == 33
    assert tokens.DEFAULT_TOKEN_LENGTH == 64


# ---- 真的 lifespan：PaaS 新服務（chart seed 只帶管理密碼）＝第一次啟動就有 token、連得上 ------------------------


@pytest.fixture
def boot(tmp_config_path, monkeypatch, synthetic_child):
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import proxy
    from mcp_admin_core.process import get_process_manager

    forwarded = []

    def upstream(request):
        forwarded.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))

    def _boot():
        monkeypatch.setattr(store_mod, "_instance", None)
        from mcp_admin_core import create_app

        app = create_app(title="Initial Token Test")
        monkeypatch.setattr(get_process_manager(), "start", AsyncMock(return_value=True))
        monkeypatch.setattr(get_process_manager(), "stop", AsyncMock(return_value=None))
        return TestClient(app)

    return _boot, forwarded


def _login(client):
    token = client.post("/api/auth/login", json={"password": "fixture-admin"}).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"


def _ring_buffer_text() -> str:
    from mcp_admin_core.routers import logs

    with logs._buffer_lock:
        return "\n".join(entry["message"] for entry in logs._log_buffer)


def test_paas_new_service_can_connect_after_the_first_start(boot, tmp_config_path, caplog, capsys):
    """chart seed 剛寫好的設定（只有管理密碼、沒有 MCP token）：第一次啟動就發 token；連得上；token 不在任何 log。"""
    tmp_config_path.write_text(json.dumps({
        "admin_password": "fixture-admin", "connection": {}, "tools": {"disabled": [], "disabled_operations": {}},
        "mcp_server": {"port": 8000}, "proxy": {"timeout": 86400},
    }))
    caplog.set_level(logging.DEBUG)
    make_client, forwarded = boot
    with make_client() as client:
        _login(client)
        cfg = json.loads(tmp_config_path.read_text())
        token = cfg["mcp_auth_token"]
        assert HEX64.match(token)
        assert cfg["token_history"][0]["action"] == "auto_issue"
        info = client.get("/api/tokens").json()
        assert info["state"] == "active" and info["token_set"] is True and token not in json.dumps(info)
        assert client.post("/api/settings/mcp_auth_token/reveal").json()["value"] == token  # 管理頁看得到完整網址
        reply = client.post(f"/private_{token}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert reply.status_code == 200 and len(forwarded) == 1
        marker = cfg["tools"]["owner_defaults_20261025"]
        assert marker["source"] == "deploy_seed"  # owner_defaults 先跑：標記照舊判成 PaaS 新服務
        view = client.get("/api/settings").json()["owner_defaults"]  # Settings 頁「已自動產生」那句的依據
        assert (view["token_state"], view["token_auto_issued"]) == ("active", True) and token not in json.dumps(view)
        assert client.post("/api/tokens/rotate", headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
        view = client.get("/api/settings").json()["owner_defaults"]
        assert (view["token_state"], view["token_auto_issued"]) == ("active", False)  # 輪替後就不是自動發的那個
        auto_token, token = token, json.loads(tmp_config_path.read_text())["mcp_auth_token"]
    with make_client() as client:  # 重啟：不再發
        assert json.loads(tmp_config_path.read_text())["mcp_auth_token"] == token
    captured = capsys.readouterr()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "auto_issue" in logged  # 有一行紀錄……
    assert sum("自動產生" in record.getMessage() for record in caplog.records) == 1
    for text in (logged, captured.out, captured.err, _ring_buffer_text()):
        assert auto_token not in text and token not in text  # ……但不含 token


def test_revoked_service_stays_blocked_across_restarts(boot, tmp_config_path):
    tmp_config_path.write_text(json.dumps({"admin_password": "fixture-admin", "mcp_auth_token": "first-token-0123456789"}))
    make_client, _ = boot
    with make_client() as client:
        _login(client)
        assert client.post("/api/tokens/revoke", headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
    revoked = json.loads(tmp_config_path.read_text())
    for _ in range(2):
        with make_client() as client:
            _login(client)
            assert client.get("/api/tokens").json()["state"] == "revoked"
            view = client.get("/api/settings").json()["owner_defaults"]
            assert (view["token_state"], view["token_auto_issued"]) == ("revoked", False)
            assert client.post("/private_first-token-0123456789/mcp", json={}).status_code == 403
    after = json.loads(tmp_config_path.read_text())
    assert {k: after[k] for k in ("mcp_auth_token", "mcp_auth_revoked", "mcp_auth_generation", "token_history")} == {
        k: revoked[k] for k in ("mcp_auth_token", "mcp_auth_revoked", "mcp_auth_generation", "token_history")}


def test_existing_service_that_never_set_a_token_gets_one(boot, tmp_config_path):
    """正式站 266 的形狀：已經有全開標記、有連線以外的設定，但開通後從沒設過 token → 換新映像第一次啟動就有 token。"""
    tmp_config_path.write_text(json.dumps({
        "admin_password": "fixture-admin", "mcp_auth_token": "", "mcp_auth_revoked": False, "mcp_auth_generation": "",
        "token_history": [], "admin_session_generation": "already-logged-in-once",
        "tools": {"disabled": [], "disabled_operations": {},
                  "owner_defaults_20261025": {"applied": "migrated", "at": "2026-10-25T15:51:01+00:00"}},
    }))
    make_client, _ = boot
    with make_client():
        pass
    cfg = json.loads(tmp_config_path.read_text())
    assert HEX64.match(cfg["mcp_auth_token"]) and cfg["token_history"][0]["action"] == "auto_issue"
    assert cfg["tools"]["owner_defaults_20261025"] == {"applied": "migrated", "at": "2026-10-25T15:51:01+00:00"}


# ---- 寫不進設定檔（PVC 滿、Longhorn 降成唯讀）：不擋啟動（審查第三輪 G4）-------------------------------------------


def _paas_266_shape() -> dict:
    """完整形狀（本 repo 的 fresh_config＋全開標記），token 空、沒有 history／generation：base 不必寫任何東西就能啟動。"""
    from mcp_admin_core.config.store import fresh_config

    cfg = fresh_config()
    cfg.update({"admin_password": "fixture-admin", "mcp_auth_token": "", "mcp_auth_revoked": False,
                "mcp_auth_generation": "", "token_history": []})
    cfg["tools"]["owner_defaults_20261025"] = {"applied": "migrated", "at": "2026-10-25T15:51:01+00:00"}
    return cfg


@pytest.fixture
def failing_writes(monkeypatch):
    """讓 ConfigStore 每次寫檔都丟 ENOSPC，記下嘗試次數。"""
    attempts = []

    def fail(self, data):
        attempts.append(1)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ConfigStore, "_write_sync", fail)
    return attempts


async def test_write_failure_skips_the_token_and_changes_nothing(tmp_config_path, failing_writes, caplog):
    from mcp_admin_core.initial_token import ensure_initial_mcp_token

    tmp_config_path.write_text(json.dumps(_paas_266_shape()))
    before = tmp_config_path.read_bytes()
    store = ConfigStore(path=tmp_config_path)
    caplog.set_level(logging.INFO)
    assert await ensure_initial_mcp_token(store) is False
    assert failing_writes == [1]
    assert tmp_config_path.read_bytes() == before
    assert (await store.load())["mcp_auth_token"] == ""  # 快取也沒換：沒有「記憶體裡有、檔案裡沒有」的 token
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "寫不進去" in r.getMessage()]
    assert len(warnings) == 1 and "OSError" in warnings[0].getMessage()
    assert not any("自動產生一個" in r.getMessage() for r in caplog.records)


async def test_unreadable_config_still_raises(tmp_config_path):
    from mcp_admin_core.config.store import ConfigUnavailable
    from mcp_admin_core.initial_token import ensure_initial_mcp_token

    tmp_config_path.write_text("{not json")
    with pytest.raises(ConfigUnavailable):
        await ensure_initial_mcp_token(ConfigStore(path=tmp_config_path))


def test_admin_starts_when_the_first_token_cannot_be_written(boot, tmp_config_path, failing_writes, capsys):
    """266 的形狀、設定檔寫不進去：e228ee1 的 lifespan 在這裡拋 OSError 起不來；現在照常啟動（與自動發 token 之前相同：
    管理頁與 /healthz 可用，用戶端連不上），寫得進去之後下一次啟動就發。"""
    tmp_config_path.write_text(json.dumps(_paas_266_shape()))
    before = tmp_config_path.read_bytes()
    make_client, forwarded = boot
    with make_client() as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/private_anything/mcp", json={}).status_code == 403 and forwarded == []
    assert failing_writes == [1] and tmp_config_path.read_bytes() == before
    assert "寫不進去" in capsys.readouterr().out  # warning 也到容器 stdout（kubectl logs）


def test_token_is_issued_once_the_config_is_writable_again(boot, tmp_config_path, monkeypatch):
    """寫入失敗那次沒發；空間恢復後的下一次啟動就發（設定仍是「從來沒有過 token」）。"""
    tmp_config_path.write_text(json.dumps(_paas_266_shape()))
    original = ConfigStore._write_sync

    def read_only(self, data):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(ConfigStore, "_write_sync", read_only)
    make_client, _ = boot
    with make_client() as client:
        assert client.get("/healthz").status_code == 200
    assert json.loads(tmp_config_path.read_text())["mcp_auth_token"] == ""
    monkeypatch.setattr(ConfigStore, "_write_sync", original)
    with make_client() as client:
        assert client.get("/healthz").status_code == 200
    cfg = json.loads(tmp_config_path.read_text())
    assert HEX64.match(cfg["mcp_auth_token"]) and [e["action"] for e in cfg["token_history"]] == ["auto_issue"]
