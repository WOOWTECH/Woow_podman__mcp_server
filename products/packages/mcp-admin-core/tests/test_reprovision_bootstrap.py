"""真實 chart seed → 產品 lifespan → 管理 API／proxy 的離線重新部署回歸。

只攔截子行程 spawn 及 proxy 上游 I/O；不替換 ConfigStore 或啟動／遷移邏輯。
MCP_REPROVISION_SEED_SCRIPT 可指定基礎設施人員確認的實際 chart 腳本。
"""

from management_client import revision_headers
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_admin_core import process, proxy
from mcp_admin_core.config import store as stores
from mcp_admin_core.config.store import ConfigUnavailable
from mcp_admin_core.owner_defaults import DECISION, MARKER

FIXTURE = Path(__file__).parent / "fixtures" / "reprovision"
# apps/odoo-manage 已封存且移出 uv workspace（#78：它的 mcp-server-odoo 0.7.1 要求 mcp<2），不再測試。
PRODUCTS = ("hermes", "n8n", "odoo", "k8s", "vk", "opendesign", "nextcloud", "litellm")
INITIAL = "fixture-initial-token"
CHANGED = "fixture-changed-token"
REISSUED = "fixture-reissued-token"
AUTH_KEYS = ("mcp_auth_token", "mcp_auth_revoked", "mcp_auth_generation", "token_history")
SAVED_POLICY = {"retired_tool": ["delete"]}
# 負責人 2026-10-25「預設全開」的標記（owner_defaults）。fixture 的 seed 預設帶著它＝已套用新預設的狀態，
# 下面既有的 reseed／restore 測試因此照舊驗「存檔的政策不被動」；沒有標記的舊狀態另見檔尾的遷移測試。
OWNER_MARKER = {"applied": "fresh_install", "at": "2026-10-25T00:00:00+00:00"}


class Deployment:
    def __init__(self, path, monkeypatch):
        self.target = path / "data" / "config.json"
        self.base = path / "config.base.json"
        self.secret = path / "config.secret.json"
        self.monkeypatch = monkeypatch
        self.launches = []
        self.forwarded = []
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(self.target))
        # Neither inherited env nor chart's CONFIG_PATH is a credential seed.
        monkeypatch.setenv("CONFIG_PATH", str(path / "unused.json"))
        monkeypatch.setenv("MCP_AUTH_TOKEN", CHANGED)
        monkeypatch.setenv("ADMIN_PASSWORD", "unused-password")
        monkeypatch.setattr(stores, "_instance", None)
        monkeypatch.setattr(process, "_instance", None)
        monkeypatch.setattr(process.asyncio, "create_subprocess_exec", self.spawn)
        from n8n_mcp_admin import tool_registry
        monkeypatch.setattr(tool_registry, "_loaded", True)

    async def spawn(self, *command, **kwargs):
        self.launches.append(command)
        # Real process-manager config resolution ran; no external service starts.
        raise FileNotFoundError("offline fixture child")

    def inputs(self, token=INITIAL, operations=None, *, legacy=True, owner_marker=True, env=None,
               disabled=("retired_whole_tool",)):
        tools = {"disabled": list(disabled), "disabled_operations": SAVED_POLICY if operations is None else operations}
        if owner_marker:
            tools[MARKER] = OWNER_MARKER
        mcp_server = {"port": 8000}
        if env is not None:
            mcp_server["env"] = env
        self.base.write_text(json.dumps({
            "connection": {}, "mcp_server": mcp_server, "tools": tools,
        }))
        secret = {"admin_password": "fixture-admin", "mcp_auth_token": token}
        if not legacy:
            secret.update(mcp_auth_revoked=False,
                mcp_auth_generation="fixture-seed-generation", token_history=[])
        self.secret.write_text(json.dumps(secret))

    def seed(self, required="admin_password,mcp_auth_token"):
        script = Path(os.environ.get("MCP_REPROVISION_SEED_SCRIPT", FIXTURE / "seed-config.py"))
        if script == FIXTURE / "seed-config.py":
            manifest = json.loads((FIXTURE / "source.json").read_text())
            assert hashlib.sha256(script.read_bytes()).hexdigest() == manifest["sha256"]
        result = subprocess.run([sys.executable, str(script)], env={**os.environ,
            "SEED_TARGET": str(self.target), "SEED_BASE": str(self.base),
            "SEED_SECRET": str(self.secret), "SEED_REQUIRE_FIELDS": "true",
            "SEED_REQUIRED_PATHS": required},
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def read(self):
        return json.loads(self.target.read_text())

    def trust_child(self):
        """The offline fixture child never starts. Since blocker 3 nothing is forwarded without a running,
        identity-verified child: stand in for one (synthetic, see managed_proxy_fixture)."""
        from managed_proxy_fixture import synthetic_manager
        self.monkeypatch.setattr(process, "_instance", synthetic_manager())

    @contextmanager
    def boot(self, product="hermes"):
        self.monkeypatch.setattr(stores, "_instance", None)
        self.monkeypatch.setattr(process, "_instance", None)
        main = importlib.reload(importlib.import_module(f"{product}_mcp_admin.main"))

        def upstream(request):
            self.forwarded.append(json.loads(request.content) if request.content else None)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                "result": {"content": [], "tools": []}})

        async def close_upstream(client):
            await client.aclose()

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.monkeypatch.setattr(proxy, "_get_client", lambda *_: upstream_client)
        try:
            # Context manager is essential: execute the actual product lifespan.
            with TestClient(main.app, raise_server_exceptions=False) as client:
                yield client
                client.portal.call(close_upstream, upstream_client)
        finally:
            if not upstream_client.is_closed:
                asyncio.run(upstream_client.aclose())


def login(client):
    response = client.post("/api/auth/login", json={"password": "fixture-admin"})
    assert response.status_code == 200
    client.headers["Authorization"] = "Bearer " + response.json()["token"]


def call(client, token, name="hermes_session", arguments=None):
    return client.post(f"/private_{token}/mcp", json={"jsonrpc": "2.0", "id": 1,
        "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    return Deployment(tmp_path, monkeypatch)


@pytest.mark.parametrize("product", PRODUCTS)
def test_seed_once_revoke_reseed_reissue_rotate_and_restart(deployment, product):
    d = deployment
    d.inputs()
    assert "seeded" in d.seed()
    with d.boot(product) as client:
        from mcp_admin_core.launch_policy import get_launch_policy
        if get_launch_policy().required_connection:
            # The seed has no connection: a child that exits without its required fields (nextcloud) is not
            # spawned until they are saved (2026-11-13); the launch path still ran up to that check.
            assert not d.launches and process.get_default_process_manager()._attempted_revision
        else:
            assert d.launches
        login(client)
        assert call(client, INITIAL, name="fixture_read").status_code == 200
        assert client.post("/api/tokens/revoke", headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
        revoked = d.read()
        assert revoked["mcp_auth_revoked"] and not revoked["mcp_auth_token"]
        assert revoked["mcp_auth_generation"] != "fixture-seed-generation"
        assert revoked["token_history"][0]["action"] == "revoke"
    # Both changed and stale seed values must be irrelevant to existing state.
    for seed_token, legacy in ((CHANGED, False), (INITIAL, True)):
        before = d.target.read_bytes()
        d.inputs(seed_token, operations={}, legacy=legacy)
        assert "skip" in d.seed()
        assert d.target.read_bytes() == before
        with d.boot(product) as client:
            login(client)
            assert client.get("/api/tokens").json()["state"] == "revoked"
            for token in (INITIAL, CHANGED):
                assert call(client, token).status_code == 403
            assert all(d.read()[key] == revoked[key] for key in (*AUTH_KEYS, "tools"))
    with d.boot(product) as client:
        login(client)
        assert client.put("/api/tokens", json={"token": REISSUED}, headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
        assert call(client, REISSUED, name="fixture_read").status_code == 200
        rotated = client.post("/api/tokens/rotate", headers=revision_headers(client, None, read_path='/api/settings')).json()["token"]
        assert call(client, REISSUED).status_code == 403
        assert call(client, rotated, name="fixture_read").status_code == 200
        active = d.read()
        assert active["mcp_auth_generation"] != revoked["mcp_auth_generation"]
        assert [event["action"] for event in active["token_history"]] == ["rotate", "set", "revoke"]
        assert all(token not in json.dumps(active["token_history"]) for token in (INITIAL, REISSUED, rotated))
    d.seed()
    with d.boot(product) as client:
        login(client)
        assert all(d.read()[key] == active[key] for key in (*AUTH_KEYS, "tools"))
        assert call(client, rotated, name="fixture_read").status_code == 200
        for token in (INITIAL, CHANGED, REISSUED):
            assert call(client, token).status_code == 403


def test_policy_api_survives_reseed_and_explicit_reissue(deployment):
    d = deployment
    d.inputs(operations={})
    d.seed()
    with d.boot() as client:
        login(client)
        response = client.put("/api/tools/operations", json={"disabled_operations": {"hermes_session": ["delete"]}}, headers=revision_headers(client, None, read_path='/api/tools'))
        assert response.status_code == 200
        assert client.post("/api/tokens/revoke", headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
    d.inputs(CHANGED, operations={})
    d.seed()
    with d.boot() as client:
        login(client)
        assert client.get("/api/tools").json()["disabled_operations"] == {"hermes_session": ["delete"]}
        assert client.put("/api/tokens", json={"token": REISSUED}, headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
        d.trust_child()
        before = len(d.forwarded)
        assert call(client, REISSUED, arguments={"action": "delete"}).json()["result"]["isError"] is True
        assert len(d.forwarded) == before
        assert call(client, REISSUED).json().get("result") is not None
        assert len(d.forwarded) == before + 1
        assert client.put("/api/tools/operations", json={"disabled_operations": {}}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
        assert call(client, REISSUED, arguments={"action": "delete"}).json().get("result") is not None
    # Explicit allow is durable too; stale seed cannot restore obsolete denies.
    d.inputs(INITIAL, operations={"hermes_session": ["delete"]})
    d.seed()
    with d.boot() as client:
        login(client)
        assert client.get("/api/tools").json()["disabled_operations"] == {}
        assert call(client, REISSUED, arguments={"action": "delete"}).json().get("result") is not None


def test_generic_restore_preserves_current_policy_and_migration_markers(deployment):
    d = deployment
    d.inputs()
    d.seed()
    with d.boot("nextcloud") as client:
        login(client)
        stale = d.read()
        # Explicitly enable tools previously disabled by Nextcloud's one-time migration.
        assert client.put("/api/tools", json={"disabled_tools": []}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
        # Use a registry-reviewed fixed operation.
        from nextcloud_mcp_admin.tool_registry import OPERATION_POLICIES
        name, spec = next(iter(OPERATION_POLICIES.items()))
        assert client.put("/api/tools/operations", json={"disabled_operations": {name: [spec.operations[0]]}}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
        assert client.post("/api/tokens/revoke", headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
        expected = d.read()
        stale["tools"].pop("nextcloud_policy_seeded", None)
        stale["proxy"] = {"timeout": 37}
        client.portal.call(stores.get_config_store().save, stale)
        assert d.read()["tools"] == expected["tools"]
        assert all(d.read()[key] == expected[key] for key in AUTH_KEYS)
        assert d.read()["proxy"]["timeout"] == 37
    d.seed()
    with d.boot("nextcloud") as client:
        login(client)
        assert d.read()["tools"] == expected["tools"]
        assert call(client, INITIAL).status_code == 403


@pytest.mark.parametrize("broken", [
    b"{broken", b"\xff", b"[]",
    json.dumps({"mcp_auth_revoked": "false"}).encode(),
    json.dumps({"token_history": ["broken"]}).encode(),
    json.dumps({"tools": {"disabled": [False]}}).encode(),
    json.dumps({"tools": {"disabled_operations": {"hermes_session": "delete"}}}).encode(),
    json.dumps({"tools": {"disabled_operations": {"hermes_session": [False]}}}).encode(),
])
@pytest.mark.parametrize("product", ["hermes", "nextcloud"])
def test_corrupt_state_is_not_reseeded_or_migrated(deployment, broken, product):
    d = deployment
    d.inputs()
    d.seed()
    d.target.write_bytes(broken)
    assert "skip" in d.seed()
    with pytest.raises(ConfigUnavailable):
        with d.boot(product):
            pytest.fail("corrupt security state reached serving lifespan")
    assert not d.launches
    assert d.target.read_bytes() == broken


def test_unreadable_state_does_not_fallback_to_seed(deployment, monkeypatch):
    d = deployment
    d.inputs()
    d.seed()
    before = d.target.read_bytes()
    read = os.open
    def unreadable(path, *args, **kwargs):
        if Path(path) == d.target:
            raise PermissionError("fixture unreadable state")
        return read(path, *args, **kwargs)
    # ConfigStore 以 O_NOFOLLOW fd 讀取；在實際開檔邊界注入失敗。
    monkeypatch.setattr(os, "open", unreadable)
    with pytest.raises(ConfigUnavailable):
        with d.boot():
            pytest.fail("unreadable config reached serving lifespan")
    assert d.target.read_bytes() == before and not d.launches


def test_dangling_state_path_is_not_treated_as_fresh_install(deployment):
    d = deployment
    d.inputs(token=CHANGED, operations={})
    d.target.parent.mkdir()
    missing = d.target.parent / "missing-state.json"
    d.target.symlink_to(missing)
    assert "skip" in d.seed()
    assert not missing.exists()
    with pytest.raises(ConfigUnavailable):
        with d.boot():
            pytest.fail("dangling state path was replaced with defaults")
    assert d.target.is_symlink() and not missing.exists() and not d.launches


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_disk_failure_during_reissue_never_activates_seed_or_new_token(deployment, monkeypatch, failure):
    d = deployment
    d.inputs()
    d.seed()
    with d.boot() as client:
        login(client)
        assert client.post("/api/tokens/revoke", headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
        before = d.target.read_bytes()
        def disk_failure(*args):
            raise OSError("fixture disk full")
        with monkeypatch.context() as m:
            m.setattr(stores.os, failure, disk_failure)
            response = client.put("/api/tokens", json={"token": REISSUED}, headers=revision_headers(client, None, read_path='/api/settings'))
            assert response.status_code >= 500
            assert call(client, INITIAL).status_code == 403
            assert call(client, REISSUED).status_code == 403
            assert d.target.read_bytes() == before
        assert not list(d.target.parent.glob(".config.json.tmp.*"))
    d.inputs(CHANGED)
    d.seed()
    with d.boot() as client:
        login(client)
        assert client.get("/api/tokens").json()["state"] == "revoked"
        for token in (INITIAL, CHANGED, REISSUED):
            assert call(client, token).status_code == 403


def test_failed_policy_relaxation_keeps_denial_after_reseed(deployment, monkeypatch):
    d = deployment
    d.inputs(operations={"hermes_session": ["delete"]})
    d.seed()
    with d.boot() as client:
        login(client)
        before = d.target.read_bytes()
        def disk_failure(*args):
            raise OSError("fixture disk full")
        with monkeypatch.context() as m:
            m.setattr(stores.os, "replace", disk_failure)
            assert client.put("/api/tools/operations", json={"disabled_operations": {}}, headers=revision_headers(client, None, read_path='/api/tools')).status_code >= 500
            assert call(client, INITIAL, arguments={"action": "delete"}).json()["result"]["isError"] is True
        assert d.target.read_bytes() == before and not d.forwarded
    d.inputs(CHANGED, operations={})
    d.seed()
    with d.boot() as client:
        assert call(client, CHANGED).status_code == 403
        assert call(client, INITIAL, arguments={"action": "delete"}).json()["result"]["isError"] is True
        assert not d.forwarded


@pytest.mark.parametrize("failure", ["fsync", "link"])
def test_failed_fresh_initialization_does_not_serve_defaults(deployment, monkeypatch, failure):
    d = deployment
    def disk_failure(*args):
        raise OSError("fixture disk full")
    monkeypatch.setattr(stores.os, failure, disk_failure)
    with pytest.raises(OSError, match="fixture disk full"):
        with d.boot():
            pytest.fail("failed initialization reached serving lifespan")
    assert not d.target.exists() and not d.launches
    assert not list(d.target.parent.glob(".config.json.tmp.*"))


def test_fresh_initialization_does_not_clobber_concurrent_state(deployment, monkeypatch):
    d = deployment
    link = stores.os.link
    winner = {"admin_password": "fixture-admin", "mcp_auth_token": "",
        "mcp_auth_revoked": True, "mcp_auth_generation": "concurrent-generation",
        "token_history": [], "tools": {"disabled_operations": SAVED_POLICY, MARKER: OWNER_MARKER}}
    def another_writer_publishes_first(source, target):
        target.write_text(json.dumps(winner))
        return link(source, target)
    monkeypatch.setattr(stores.os, "link", another_writer_publishes_first)
    with d.boot() as client:
        login(client)
        assert client.get("/api/tokens").json()["state"] == "revoked"
        assert call(client, INITIAL).status_code == 403
        assert call(client, CHANGED).status_code == 403
        stored = d.read()
        assert all(stored[key] == winner[key] for key in AUTH_KEYS)
        assert stored["tools"]["disabled_operations"] == SAVED_POLICY
    assert not list(d.target.parent.glob(".config.json.tmp.*"))


def test_existing_state_does_not_require_readable_or_valid_seed_fragments(deployment):
    d = deployment
    d.inputs()
    d.seed()
    with d.boot() as client:
        login(client)
        assert client.post("/api/tokens/revoke", headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
    before = d.target.read_bytes()
    d.base.write_text("malformed replacement base")
    d.secret.unlink()
    assert "skip" in d.seed()
    assert d.target.read_bytes() == before
    with d.boot() as client:
        assert call(client, INITIAL).status_code == 403
        assert d.target.read_bytes() == before


@pytest.mark.parametrize("product", ["nextcloud", "litellm", "odoo", "opendesign"])
def test_restore_does_not_prevent_product_default_and_marker_migrations(deployment, product):
    d = deployment
    d.inputs()
    if product == "opendesign":
        base = json.loads(d.base.read_text())
        base["mcp_server"].update(command="supergateway",
            args=["--stdio", "python -m opendesign_mcp_server"])
        d.base.write_text(json.dumps(base))
    d.seed()
    # Exercise a restore while the file still lacks this product's defaults.
    # Hermes has no startup migration, so it leaves that legacy shape intact.
    with d.boot("hermes") as client:
        login(client)
        stale = d.read()
        assert client.post("/api/tokens/revoke", headers=revision_headers(client, None, read_path='/api/settings')).status_code == 200
        expected = d.read()
        client.portal.call(stores.get_config_store().save, stale)
    with d.boot(product):
        migrated = d.read()
        assert all(migrated[key] == expected[key] for key in AUTH_KEYS)
        assert migrated["tools"]["disabled_operations"] == SAVED_POLICY
        if product == "nextcloud":
            # 沒有標記＝從未以 nextcloud 啟動過：新安裝全開（2026-10-25），只寫標記、不停用工具，保留既有停用項。
            assert migrated["tools"]["nextcloud_policy_seeded"] is True
            assert "delete_file_checked" not in migrated["tools"]["disabled"]
            assert "retired_whole_tool" in migrated["tools"]["disabled"]
        elif product == "litellm":
            # 缺鍵＝從未以 litellm 啟動過：新安裝全開（2026-10-25）。
            assert migrated["mcp_server"]["env"]["LITELLM_MCP_READONLY"] == "0"
        elif product == "odoo":
            # odoo 早在這些開關之前就上線：部署 seed 建立的設定缺鍵一律視為既有設定，維持改版前的值。
            assert migrated["mcp_server"]["env"]["ODOO_MCP_ALLOW_DIRECT_WRITES"] == "0"
            assert migrated["mcp_server"]["env"]["ODOO_MCP_ALLOW_UNKNOWN_METHODS"] == "0"
            assert migrated["mcp_server"]["env"]["MCP_CHATTER_DIRECT"] == "0"
        else:
            assert migrated["mcp_server"]["command"] == "opendesign-mcp-server"
    before = d.target.read_bytes()
    d.inputs(CHANGED, operations={})
    d.seed()
    with d.boot(product):
        assert d.target.read_bytes() == before, "migration repeated after reseed/restart"


# ---------------------------------------------------------------------------
# 負責人 2026-10-25「預設全開」：沒有標記的既有狀態第一次以新映像啟動 → 一次性全開；之後的限制保留
# ---------------------------------------------------------------------------

# 改版前各產品的限制性設定（舊範本 seed／出廠政策），與新映像要改成的全開值（＝各 app 的 LaunchPolicy.fresh_env）。
_OLD_ENV = {
    "odoo": {"ODOO_MCP_ENABLE_WRITES": "1", "ODOO_MCP_ALLOW_DIRECT_WRITES": "0"},
    "k8s": {"K8S_MCP_READ_ONLY": "1", "K8S_MCP_DISABLE_DESTRUCTIVE": "1", "K8S_MCP_ALLOW_SECRETS": "0",
            "K8S_MCP_TOOLSETS": "core,config,helm"},
    "litellm": {"LITELLM_MCP_READONLY": "1"},
}
_ALL_ON_ENV = {
    "odoo": {"ODOO_MCP_ENABLE_WRITES": "1", "ODOO_MCP_ALLOW_DIRECT_WRITES": "1",
             "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "1", "MCP_CHATTER_DIRECT": "1"},
    "k8s": {"K8S_MCP_READ_ONLY": "0", "K8S_MCP_DISABLE_DESTRUCTIVE": "0", "K8S_MCP_ALLOW_SECRETS": "1",
            "K8S_MCP_TOOLSETS": "config,core,helm,kcp,kubevirt,tekton", "K8S_MCP_MULTI_CLUSTER": "1"},
    "litellm": {"LITELLM_MCP_READONLY": "0"},
}
# 租戶在遷移後自己關掉的開關（要跨重啟保留）。
_TENANT_RESTRICTION = {
    "odoo": ("ODOO_MCP_ALLOW_DIRECT_WRITES", "0"),
    "k8s": ("K8S_MCP_READ_ONLY", "1"),
    "litellm": ("LITELLM_MCP_READONLY", "1"),
}
# 舊 nextcloud 出廠政策：停用 4 支寫入／刪除工具，並已寫上 app 自己的標記。
_NEXTCLOUD_OLD_DISABLED = ("create_text_file", "update_text_file", "upload_file", "delete_file_checked")


@pytest.mark.parametrize("product", PRODUCTS)
def test_existing_state_without_marker_becomes_all_on_once_and_later_restrictions_stick(deployment, product, caplog):
    d = deployment
    disabled = _NEXTCLOUD_OLD_DISABLED if product == "nextcloud" else ("retired_whole_tool",)
    d.inputs(owner_marker=False, env=_OLD_ENV.get(product), disabled=disabled)
    d.seed()
    if product == "nextcloud":
        old = d.read()
        old["tools"]["nextcloud_policy_seeded"] = True
        d.target.write_text(json.dumps(old))
    before = d.read()
    with d.boot(product) as client:
        login(client)
        migrated = d.read()
        assert migrated["tools"]["disabled"] == [] and migrated["tools"]["disabled_operations"] == {}
        assert migrated["tools"][MARKER]["applied"] == "migrated"
        for key, value in _ALL_ON_ENV.get(product, {}).items():
            assert migrated["mcp_server"]["env"][key] == value, key
        for key in (*AUTH_KEYS, "admin_password", "connection"):
            default = stores._DEFAULT_CONFIG.get(key)
            assert migrated.get(key, default) == before.get(key, default), key
        assert migrated["mcp_server"]["port"] == before["mcp_server"]["port"]
        assert client.get("/api/settings").json()["owner_defaults"]["applied"] == "migrated"
        assert any(DECISION in r.getMessage() for r in caplog.records)
        # 租戶之後自己收緊：停用一支工具、關掉一個開關（Settings 頁）
        tool = client.get("/api/tools").json()["tools"][0]["name"]
        assert client.put("/api/tools", json={"disabled_tools": [tool]}, headers=revision_headers(client, None, read_path='/api/tools')).status_code == 200
        restrict = _TENANT_RESTRICTION.get(product)
        if restrict:
            current = client.get("/api/settings").json()["mcp_server"]["env"]
            response = client.put("/api/settings/mcp_server", json={"env": {**current, restrict[0]: restrict[1]}}, headers=revision_headers(client, None, read_path='/api/settings'))
            assert response.status_code == 200, response.text
    restricted = d.read()
    assert restricted["tools"]["disabled"] == [tool]
    if restrict:
        assert restricted["mcp_server"]["env"][restrict[0]] == restrict[1]
    # 重啟、重新 seed（chart seed-if-absent 跳過）、再升級一次：都不再改
    assert "skip" in d.seed()
    for _ in range(2):
        with d.boot(product):
            pass
        assert d.read() == restricted


@pytest.mark.parametrize("product", ["hermes", "odoo", "nextcloud"])
def test_state_with_marker_is_never_migrated_again(deployment, product):
    d = deployment
    d.inputs(env=_OLD_ENV.get(product))
    d.seed()
    with d.boot(product):
        pass
    first = d.read()
    assert first["tools"][MARKER] == OWNER_MARKER
    assert first["tools"]["disabled_operations"] == SAVED_POLICY
    with d.boot(product):
        pass
    assert d.read() == first


@pytest.mark.parametrize("product", PRODUCTS)
def test_paas_new_service_is_marked_as_a_deploy_seed(deployment, product):
    """PaaS 新開的服務（chart seed initContainer 剛寫好 config.json、admin 第一次啟動）：標記 fresh_install＋
    deploy_seed，Settings 不能說它是「既有設定…升級時只寫上標記」。

    輸入照 PaaS 實際形狀：base＝chart 0.1.9 values 預設（connection 兩個空字串、tools 兩個空清單、proxy.timeout）＋
    平台範本的全開 env（＝各 app 的 LaunchPolicy.fresh_env）；secret＝平台 config.sensitive（只有 admin_password），
    n8n 另有 chart 自產的 bearer。平台不 seed MCP token，所以 seed 的必要欄位只有 admin_password（範本 requiredFields）。
    """
    d = deployment
    fresh_env = dict(importlib.import_module(f"{product}_mcp_admin.main").LAUNCH_POLICY.fresh_env)
    d.base.write_text(json.dumps({
        "connection": {"gateway_url": "", "dashboard_url": ""},
        "tools": {"disabled": [], "disabled_operations": {}},
        "mcp_server": {"port": 8000, "env": fresh_env},
        "proxy": {"timeout": 86400},
    }))
    secret = {"admin_password": "fixture-admin"}
    if product == "n8n":
        secret.update(mcp_server={"env": {"AUTH_TOKEN": "fixture-chart-bearer"}},
                      proxy={"bearer_token": "fixture-chart-bearer"})
    d.secret.write_text(json.dumps(secret))
    d.seed(required="admin_password")
    with d.boot(product) as client:
        login(client)
        view = client.get("/api/settings").json()["owner_defaults"]
        # 第三輪 G4：平台不 seed MCP token，第一次啟動就自動發一個（同輪替的產生器與記帳），不必先到 Tokens 頁輪替
        stored = d.read()
        token = stored["mcp_auth_token"]
        assert len(token) == 64 and all(c in "0123456789abcdef" for c in token)
        assert [event["action"] for event in stored["token_history"]] == ["auto_issue"]
        assert client.get("/api/tokens").json()["state"] == "active"
        assert call(client, token, name="fixture_read").status_code == 200
    marker = d.read()["tools"][MARKER]
    assert (marker["applied"], marker["source"]) == ("fresh_install", "deploy_seed")
    assert (view["applied"], view["source"]) == ("fresh_install", "deploy_seed")
    with d.boot(product):  # 重啟不再發
        assert d.read()["mcp_auth_token"] == token
