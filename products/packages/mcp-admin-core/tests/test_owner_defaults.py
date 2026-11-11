"""負責人 2026-10-25「預設全開」：既有 config.json 第一次以新映像啟動時一次性改成全開（owner_defaults）。

- 沒有標記的既有設定 → 全開（env 開關＝LaunchPolicy.fresh_env、tools.disabled／disabled_operations 清空）＋標記，
  憑證、token、連線、proxy 等一律不動；記一行 log。
- 有標記 → 什麼都不做（之後管理員的限制跨重啟、整份還原都保留）。
- 全新安裝 → 建檔時就有標記，不需遷移。
- 壞檔／懸空 symlink／型別錯 → ConfigUnavailable，不遷移、不覆寫。
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from mcp_admin_core.config.store import ConfigStore, ConfigUnavailable
from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
from mcp_admin_core.owner_defaults import DECISION, MARKER, apply_owner_defaults, owner_defaults_view

POLICY = LaunchPolicy(
    command="x",
    connection_keys=frozenset({"base_url", "api_key"}),
    env_keys=frozenset({"A_WRITES", "B_READONLY", "C_TOOLSETS"}),
    editable_env_keys=frozenset({"A_WRITES", "B_READONLY"}),
    fresh_env={"A_WRITES": "1", "B_READONLY": "0", "C_TOOLSETS": "core,extra"},
)

RESTRICTIVE = {
    "admin_password": "fixture-admin-password",
    "mcp_auth_token": "fixture-mcp-token-0123456789",
    "mcp_auth_revoked": False,
    "mcp_auth_generation": "fixture-generation",
    "connection": {"base_url": "https://backend.example.invalid", "api_key": "fixture-api-key"},
    "tools": {"disabled": ["delete_everything", "write_thing"], "disabled_operations": {"thing": ["delete"]},
              "product_policy_seeded": True},
    "mcp_server": {"command": "x", "args": [], "port": 8123, "env_prefix": "P_",
                   "env": {"A_WRITES": "0", "B_READONLY": "1", "C_TOOLSETS": "core", "AUTH_TOKEN": "keep-me"}},
    "proxy": {"timeout": 37, "bearer_token": "fixture-bearer"},
    "token_history": [{"action": "set", "rotated_at": "2026-10-01T00:00:00+00:00", "previous_token_masked": ""}],
}


@pytest.fixture
def policy():
    set_launch_policy(POLICY)  # conftest 的 autouse fixture 每個測試後還原
    return POLICY


def _write(path, data):
    path.write_text(json.dumps(data))
    return path.read_bytes()


async def test_existing_restrictive_config_becomes_all_on_once(tmp_config_path, policy, caplog):
    _write(tmp_config_path, RESTRICTIVE)
    store = ConfigStore(path=tmp_config_path)
    caplog.set_level(logging.INFO, logger="mcp_admin_core.owner_defaults")

    changes = await apply_owner_defaults(store)

    assert changes and any("A_WRITES" in c for c in changes)
    cfg = json.loads(tmp_config_path.read_text())
    assert cfg["mcp_server"]["env"] == {"A_WRITES": "1", "B_READONLY": "0", "C_TOOLSETS": "core,extra",
                                        "AUTH_TOKEN": "keep-me"}
    assert cfg["tools"]["disabled"] == [] and cfg["tools"]["disabled_operations"] == {}
    assert cfg["tools"]["product_policy_seeded"] is True, "產品自己的標記不動"
    assert cfg["tools"][MARKER]["applied"] == "migrated" and cfg["tools"][MARKER]["at"]
    assert cfg["tools"][MARKER]["source"] == "existing_config"
    for key in ("admin_password", "mcp_auth_token", "mcp_auth_revoked", "mcp_auth_generation", "connection",
                "proxy", "token_history"):
        assert cfg[key] == RESTRICTIVE[key], key
    for key in ("command", "args", "port", "env_prefix"):
        assert cfg["mcp_server"][key] == RESTRICTIVE["mcp_server"][key], key
    assert tmp_config_path.stat().st_mode & 0o777 == 0o600
    logged = [r.getMessage() for r in caplog.records if r.name == "mcp_admin_core.owner_defaults"]
    assert len(logged) == 1 and DECISION in logged[0]
    for secret in ("fixture-api-key", "fixture-mcp-token", "fixture-bearer", "fixture-admin-password", "keep-me"):
        assert secret not in logged[0]


async def test_marker_present_is_a_noop(tmp_config_path, policy):
    data = json.loads(json.dumps(RESTRICTIVE))
    data["tools"][MARKER] = {"applied": "migrated", "at": "2026-10-25T00:00:00+00:00"}
    before = _write(tmp_config_path, data)
    assert await apply_owner_defaults(ConfigStore(path=tmp_config_path)) is None
    assert tmp_config_path.read_bytes() == before


async def test_tenant_restrictions_after_migration_survive_restart_and_restore(tmp_config_path, policy):
    _write(tmp_config_path, RESTRICTIVE)
    store = ConfigStore(path=tmp_config_path)
    await apply_owner_defaults(store)
    stale = await store.load()
    stale["tools"].pop(MARKER)
    # 租戶之後自己收緊（Tools 頁／Settings 頁）
    await store.patch("tools", {"disabled": ["write_thing"], "disabled_operations": {"thing": ["delete"]}})
    env = dict((await store.get("mcp_server"))["env"], B_READONLY="1")
    await store.patch("mcp_server", {"env": env})
    # 整份還原（舊快照沒有標記）也不能讓遷移重跑：save 保留目前的 tools（含標記）
    await store.save(stale)
    expected_tools = (await store.load())["tools"]
    assert expected_tools["disabled"] == ["write_thing"] and MARKER in expected_tools
    # 重啟（新的 store 行程）、再升級一次：都不再改
    before = tmp_config_path.read_bytes()
    for _ in range(2):
        assert await apply_owner_defaults(ConfigStore(path=tmp_config_path)) is None
    assert tmp_config_path.read_bytes() == before


async def test_fresh_install_writes_marker_at_creation(tmp_config_path, policy):
    store = ConfigStore(path=tmp_config_path)
    created = json.loads(tmp_config_path.read_text())
    assert created["tools"][MARKER]["applied"] == "fresh_install"
    assert created["tools"][MARKER]["source"] == "new_config"
    assert created["mcp_server"]["env"] == dict(POLICY.fresh_env)
    before = tmp_config_path.read_bytes()
    assert await apply_owner_defaults(store) is None
    assert tmp_config_path.read_bytes() == before


async def test_seeded_config_that_is_already_all_on_only_gets_the_marker(tmp_config_path, policy, caplog):
    """部署 seed 建立、還沒有標記的新設定：本來就是全開 → 只寫標記（記 info，不記遷移警告）。"""
    _write(tmp_config_path, {"tools": {"disabled": [], "disabled_operations": {}},
                             "mcp_server": {"env": dict(POLICY.fresh_env)}})
    caplog.set_level(logging.INFO, logger="mcp_admin_core.owner_defaults")
    assert await apply_owner_defaults(ConfigStore(path=tmp_config_path)) == []
    cfg = json.loads(tmp_config_path.read_text())
    assert cfg["tools"][MARKER]["applied"] == "fresh_install"
    # 不需要改：applied 照舊是 fresh_install；還沒有任何使用痕跡，source 是部署 seed（不是既有設定）
    assert cfg["tools"][MARKER]["source"] == "deploy_seed"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# PaaS 新開的服務第一次啟動時的 config.json：chart 0.1.9 的 values 預設（connection 兩個空字串、tools 兩個空清單、
# proxy.timeout）＋平台範本的 mcp_server＋平台 config.sensitive（只有 admin_password）；n8n 另有 chart 自產的
# bearer（proxy.bearer_token＝mcp_server.env.AUTH_TOKEN）。
PAAS_NEW_SERVICE_SEED = {
    "admin_password": "fixture-platform-password",
    "connection": {"gateway_url": "", "dashboard_url": ""},
    "tools": {"disabled": [], "disabled_operations": {}},
    "mcp_server": {"command": "x", "args": ["--port", "8000"], "port": 8000, "env_prefix": "P_",
                   "env": {**POLICY.fresh_env, "AUTH_TOKEN": "fixture-chart-bearer"}},
    "proxy": {"timeout": 86400, "bearer_token": "fixture-chart-bearer"},
}


async def test_paas_new_service_seed_is_a_deploy_seed_not_an_existing_config(tmp_config_path, policy):
    _write(tmp_config_path, PAAS_NEW_SERVICE_SEED)
    assert await apply_owner_defaults(ConfigStore(path=tmp_config_path)) == []
    cfg = json.loads(tmp_config_path.read_text())
    assert {k: v for k, v in cfg["tools"][MARKER].items() if k != "at"} == {
        "applied": "fresh_install", "source": "deploy_seed"}
    view = owner_defaults_view(cfg)
    assert view["applied"] == "fresh_install" and view["source"] == "deploy_seed"


@pytest.mark.parametrize(("key", "value"), [
    ("mcp_auth_token", "fixture-set-from-the-admin-page"),  # 10-11 之前第一次設 token 不會留 history
    ("token_history", [{"action": "rotate", "rotated_at": "t", "previous_token_masked": "ab***cd"}]),
    ("mcp_auth_generation", "fixture-generation"),
    ("mcp_auth_revoked", True),
    ("admin_session_generation", "fixture-admin-session"),  # 10-11 起登入或改密碼就會寫
    ("connection", {"gateway_url": "", "base_url": "https://backend.example.invalid"}),
    ("connection", {"in_cluster": "0"}),
    ("connection", {"mcp_max_sessions": 20}),  # 非字串的值也算設定過
])
async def test_any_trace_of_use_makes_it_an_existing_config(tmp_config_path, policy, key, value):
    data = json.loads(json.dumps(PAAS_NEW_SERVICE_SEED))
    data[key] = value
    _write(tmp_config_path, data)
    assert await apply_owner_defaults(ConfigStore(path=tmp_config_path)) == []
    marker = json.loads(tmp_config_path.read_text())["tools"][MARKER]
    assert marker["applied"] == "fresh_install" and marker["source"] == "existing_config"


@pytest.mark.parametrize(("key", "value"), [
    ("mcp_auth_token", ""), ("mcp_auth_token", "   "), ("token_history", []), ("mcp_auth_generation", ""),
    ("mcp_auth_revoked", False), ("admin_session_generation", ""), ("connection", {}),
    ("connection", {"gateway_url": " ", "dashboard_url": None, "extra": [], "nested": {}}),
])
async def test_blank_values_are_not_traces_of_use(tmp_config_path, policy, key, value):
    """store 寫檔時會把共用預設（mcp_auth_token=""、token_history=[]…）一起寫進去：空值不算使用痕跡。"""
    data = json.loads(json.dumps(PAAS_NEW_SERVICE_SEED))
    data[key] = value
    _write(tmp_config_path, data)
    await apply_owner_defaults(ConfigStore(path=tmp_config_path))
    assert json.loads(tmp_config_path.read_text())["tools"][MARKER]["source"] == "deploy_seed"


async def test_untouched_seed_with_restrictions_is_migrated_and_keeps_the_seed_source(tmp_config_path, policy):
    """10-25 之前開通、從沒有人用過的服務（舊範本 seed 有限制）：照樣一次性改成全開；source 說明它還沒被用過。"""
    data = json.loads(json.dumps(PAAS_NEW_SERVICE_SEED))
    data["mcp_server"]["env"].update(A_WRITES="0")
    data["tools"]["disabled"] = ["write_thing"]
    _write(tmp_config_path, data)
    changes = await apply_owner_defaults(ConfigStore(path=tmp_config_path))
    assert changes
    marker = json.loads(tmp_config_path.read_text())["tools"][MARKER]
    assert marker["applied"] == "migrated" and marker["source"] == "deploy_seed"


async def test_product_startup_defaults_written_before_the_migration_are_not_traces_of_use(tmp_config_path, policy):
    """產品的 seed_defaults（odoo／litellm／nextcloud／opendesign）在 core lifespan 之前以 store.patch 寫檔，
    會把共用預設的空 token／空 history 一起寫進去——第一次啟動的 PaaS 新服務仍是部署 seed。"""
    _write(tmp_config_path, PAAS_NEW_SERVICE_SEED)
    store = ConfigStore(path=tmp_config_path)
    await store.patch("tools", {"product_policy_seeded": True})
    await store.patch("mcp_server", {"env_prefix": "P_"})
    written = json.loads(tmp_config_path.read_text())
    assert written["mcp_auth_token"] == "" and written["token_history"] == []  # 共用預設確實落盤了
    await apply_owner_defaults(store)
    assert json.loads(tmp_config_path.read_text())["tools"][MARKER]["source"] == "deploy_seed"


@pytest.mark.parametrize("broken", [
    b"{broken", b"[]",
    json.dumps({"tools": {"disabled": [False]}}).encode(),
    json.dumps({"tools": "all"}).encode(),
    json.dumps({"mcp_server": "x"}).encode(),
])
async def test_corrupt_config_fails_safe(tmp_config_path, policy, broken):
    tmp_config_path.write_bytes(broken)
    with pytest.raises(ConfigUnavailable):
        await apply_owner_defaults(ConfigStore(path=tmp_config_path))
    assert tmp_config_path.read_bytes() == broken


async def test_dangling_symlink_is_not_replaced(tmp_path, policy):
    """懸空 symlink 是讀不到的既有狀態，不是新安裝：不建檔、不遷移、不換掉 symlink。"""
    link = tmp_path / "config.json"
    link.symlink_to(tmp_path / "missing" / "config.json")
    store = ConfigStore(path=link)
    with pytest.raises(ConfigUnavailable):
        await apply_owner_defaults(store)
    assert link.is_symlink() and not (tmp_path / "missing").exists()


def test_lifespan_migrates_before_the_child_starts(make_app, tmp_config_path, monkeypatch):
    """core lifespan：子行程啟動那一刻，設定已是全開；管理頁 Settings 顯示已套用。"""
    from fastapi.testclient import TestClient

    from mcp_admin_core.config import get_config_store
    from mcp_admin_core.process import get_process_manager

    make_app(json.loads(json.dumps(RESTRICTIVE)))
    from mcp_admin_core import create_app

    app = create_app(title="Owner Defaults Test", launch_policy=POLICY)
    seen = {}

    async def fake_start():
        seen.update(await get_config_store().load())
        return True

    monkeypatch.setattr(get_process_manager(), "start", AsyncMock(side_effect=fake_start))
    monkeypatch.setattr(get_process_manager(), "stop", AsyncMock(return_value=None))
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"password": "fixture-admin-password"}).json()["token"]
        body = client.get("/api/settings", headers={"Authorization": f"Bearer {token}"}).json()
    assert seen["mcp_server"]["env"]["A_WRITES"] == "1" and seen["tools"]["disabled"] == []
    assert body["owner_defaults"]["applied"] == "migrated" and body["owner_defaults"]["decision"] == DECISION


def test_owner_defaults_view():
    assert owner_defaults_view({"tools": {}}) is None
    # fc5fa02 寫的舊標記沒有 source：照讀，source 為 None（不知道）
    assert owner_defaults_view({"tools": {MARKER: {"applied": "migrated", "at": "t"}}}) == {
        "decision": DECISION, "applied": "migrated", "source": None, "at": "t",
        "token_state": "unconfigured", "token_auto_issued": False}
    assert owner_defaults_view({"tools": {MARKER: True}})["applied"] == "fresh_install"
    for source in ("new_config", "deploy_seed", "existing_config"):
        assert owner_defaults_view({"tools": {MARKER: {"applied": "fresh_install", "source": source,
                                                       "at": "t"}}})["source"] == source
    assert owner_defaults_view({"tools": {MARKER: {"applied": "fresh_install", "source": "<b>x</b>"}}})["source"] is None


AUTO = {"action": "auto_issue", "rotated_at": "t", "previous_token_masked": ""}
ROTATE = {"action": "rotate", "rotated_at": "t2", "previous_token_masked": "ab***cd"}
REVOKE = {"action": "revoke", "rotated_at": "t3", "previous_token_masked": "ab***cd"}


@pytest.mark.parametrize(("state", "expected"), [
    ({"mcp_auth_token": "fixture-token", "token_history": [AUTO]}, ("active", True)),  # 第一次啟動自動發、還在用
    ({"mcp_auth_token": "fixture-token-2", "token_history": [ROTATE, AUTO]}, ("active", False)),  # 之後輪替過
    ({"mcp_auth_token": "fixture-token"}, ("active", False)),  # 第二輪映像開出、管理員自己設的（沒有 auto_issue）
    ({"mcp_auth_token": "fixture-token", "token_history": [ROTATE]}, ("active", False)),
    ({"mcp_auth_token": "", "mcp_auth_revoked": True, "token_history": [REVOKE, AUTO]}, ("revoked", False)),
    ({"mcp_auth_token": "", "token_history": []}, ("unconfigured", False)),  # 例如自動發時設定檔寫不進去
    ({"mcp_auth_token": "   ", "token_history": [AUTO]}, ("unconfigured", False)),
])
def test_owner_defaults_view_reports_the_token_facts(state, expected):
    """Settings 頁「已自動產生 token」那句只在現在的 token 真的是自動發的時候才說；撤銷／沒有 token 另外說（審查第三輪 G4）。"""
    view = owner_defaults_view({"tools": {MARKER: {"applied": "fresh_install", "source": "deploy_seed", "at": "t"}},
                                **state})
    assert (view["token_state"], view["token_auto_issued"]) == expected
    assert "fixture-token" not in json.dumps(view)  # 不帶 token


async def test_new_markers_stay_readable_for_old_readers(tmp_config_path, policy):
    """讀標記的舊程式（例如驗收的 check_marker.py：json.load 後照印 tools.owner_defaults_20261025）不能壞：
    applied 仍是 fresh_install／migrated、at 仍在，只多一個 source。"""
    _write(tmp_config_path, RESTRICTIVE)
    await apply_owner_defaults(ConfigStore(path=tmp_config_path))
    marker = json.loads(tmp_config_path.read_text())["tools"][MARKER]
    assert set(marker) == {"applied", "source", "at"}
    assert marker["applied"] in ("fresh_install", "migrated") and isinstance(marker["at"], str)
    assert json.loads(json.dumps(marker)) == marker  # 純 JSON 字串值


# ---- 遷移紀錄也進 stdout（kubectl logs）、ring buffer 只一次（第二輪驗證 F4）---------------------------


def _ring_buffer_lines(text: str) -> list[dict]:
    from mcp_admin_core.routers import logs

    with logs._buffer_lock:
        return [entry for entry in logs._log_buffer if text in entry["message"]]


@pytest.fixture
def admin_log_capture():
    """create_logs_router() 在 mcp_admin_core 掛的 ring buffer handler（正式 app 匯入時就掛好）。"""
    from mcp_admin_core.routers import logs

    logs._install_admin_log_capture(("mcp_admin_core",))
    with logs._buffer_lock:
        logs._log_buffer.clear()
    return logs


async def test_migration_line_is_on_stdout_and_once_in_the_ring_buffer(tmp_config_path, policy, capsys,
                                                                        admin_log_capture):
    _write(tmp_config_path, RESTRICTIVE)
    await apply_owner_defaults(ConfigStore(path=tmp_config_path))
    stdout = [line for line in capsys.readouterr().out.splitlines() if DECISION in line]
    assert len(stdout) == 1
    assert stdout[0].startswith("WARNING:") and "[mcp_admin_core.owner_defaults]" in stdout[0]
    assert "tools.disabled (2 → 0)" in stdout[0] and "mcp_server.env[A_WRITES, B_READONLY, C_TOOLSETS]" in stdout[0]
    buffered = _ring_buffer_lines(DECISION)
    assert len(buffered) == 1 and buffered[0]["source"] == "mcp_admin_core.owner_defaults"
    assert buffered[0]["level"] == "warning"
    for secret in ("fixture-api-key", "fixture-mcp-token", "fixture-bearer", "fixture-admin-password", "keep-me"):
        assert secret not in stdout[0] and secret not in buffered[0]["message"]


async def test_marker_only_line_is_printed_too(tmp_config_path, policy, capsys, admin_log_capture):
    _write(tmp_config_path, {"tools": {"disabled": [], "disabled_operations": {}},
                             "mcp_server": {"env": dict(POLICY.fresh_env)}})
    assert await apply_owner_defaults(ConfigStore(path=tmp_config_path)) == []
    stdout = [line for line in capsys.readouterr().out.splitlines() if DECISION in line]
    assert len(stdout) == 1 and stdout[0].startswith("INFO:") and MARKER in stdout[0]
    assert len(_ring_buffer_lines(DECISION)) == 1


async def test_no_line_when_the_marker_is_already_there(tmp_config_path, policy, capsys, admin_log_capture):
    data = json.loads(json.dumps(RESTRICTIVE))
    data["tools"][MARKER] = {"applied": "migrated", "at": "2026-10-25T00:00:00+00:00"}
    _write(tmp_config_path, data)
    assert await apply_owner_defaults(ConfigStore(path=tmp_config_path)) is None
    assert DECISION not in capsys.readouterr().out
    assert _ring_buffer_lines(DECISION) == []


def test_stdout_handler_is_installed_once():
    import importlib

    from mcp_admin_core import owner_defaults

    owner_defaults._install_stdout_audit()
    owner_defaults._install_stdout_audit()
    handlers = [h for h in owner_defaults.logger.handlers if isinstance(h, owner_defaults._StdoutHandler)]
    assert len(handlers) == 1
    assert importlib.import_module("mcp_admin_core.owner_defaults") is owner_defaults


# ---- 清單鍵取聯集（第二輪驗證 F5：k8s K8S_MCP_TOOLSETS 不再整個覆寫）--------------------------------------

UNION_POLICY = LaunchPolicy(
    command="x",
    env_keys=frozenset({"A_WRITES", "C_TOOLSETS"}),
    fresh_env={"A_WRITES": "1", "C_TOOLSETS": "config,core,helm"},
    fresh_env_union_keys=frozenset({"C_TOOLSETS"}),
)


def _legacy(env: dict) -> dict:
    return {"tools": {"disabled": [], "disabled_operations": {}}, "mcp_server": {"env": env}}


@pytest.mark.parametrize(("before", "after"), [
    ("core,kiali", "config,core,helm,kiali"),             # 全開值的順序為準，舊值多的項目照原順序補在後面
    (" core , kiali ,, core", "config,core,helm,kiali"),  # 去空白、去重
    ("netobserv,core,kiali", "config,core,helm,netobserv,kiali"),
    ("helm,core,config,kiali", "helm,core,config,kiali"),  # 已經涵蓋全開項目：照舊值，不動順序
    ("core,config", "config,core,helm"),                  # 改版前的出廠值：正好變成新安裝的全開值
    (None, "config,core,helm"),                           # 缺鍵：等於全開值（與新安裝相同）
    ("", "config,core,helm"),
])
async def test_list_keys_are_unioned_not_overwritten(tmp_config_path, before, after):
    from mcp_admin_core.owner_defaults import union_list_value

    set_launch_policy(UNION_POLICY)
    env = {"A_WRITES": "0"} if before is None else {"A_WRITES": "0", "C_TOOLSETS": before}
    _write(tmp_config_path, _legacy(env))
    changes = await apply_owner_defaults(ConfigStore(path=tmp_config_path))
    saved = json.loads(tmp_config_path.read_text())["mcp_server"]["env"]
    assert saved == {"A_WRITES": "1", "C_TOOLSETS": after}
    assert ("C_TOOLSETS" in changes[0]) is (before != after)
    assert union_list_value(after, "config,core,helm") == after  # 再套一次不變


def test_union_keys_must_be_fresh_env_keys():
    with pytest.raises(ValueError, match="fresh_env_union_keys"):
        LaunchPolicy(command="x", env_keys=frozenset({"A"}), fresh_env={"A": "1"},
                     fresh_env_union_keys=frozenset({"B"}))


async def test_policies_without_union_keys_still_overwrite(tmp_config_path, policy):
    """其他產品（沒有宣告清單鍵）照舊：全開值整個覆寫。"""
    _write(tmp_config_path, RESTRICTIVE)
    await apply_owner_defaults(ConfigStore(path=tmp_config_path))
    assert json.loads(tmp_config_path.read_text())["mcp_server"]["env"]["C_TOOLSETS"] == "core,extra"
