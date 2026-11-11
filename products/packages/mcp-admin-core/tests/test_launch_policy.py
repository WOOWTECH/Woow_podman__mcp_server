"""#1530：子行程 command／args 固定、connection／env 白名單、管理 API 不能改子行程指令。

背景：mcp-admin-core 的 ``PUT /api/settings/mcp_server`` 曾接受任意 command／args／env，存檔後直接
``create_subprocess_exec``；``PUT /api/settings/connection`` 的任意鍵也會變成子行程環境變數
（例如 ``webhook_security_mode=permissive`` 可關掉 n8n-mcp 的 SSRF 防護、``node_options`` 可載入任意程式）。
"""
from __future__ import annotations

from management_client import revision_headers

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from mcp_admin_core.launch_policy import LaunchPolicy, is_blocked_env_name, set_launch_policy
from mcp_admin_core.process import McpProcessManager, _build_subprocess_env, resolve_launch


def _policy(**kw) -> LaunchPolicy:
    base = dict(
        command="app-mcp",
        args=("--port", "{port}"),
        env_prefix="",
        connection_keys=frozenset({"app_url", "app_key", "session_s"}),
        non_env_connection_keys=frozenset({"session_s"}),
        env_keys=frozenset({"AUTH_TOKEN", "APP_FLAG"}),
        editable_env_keys=frozenset({"APP_FLAG"}),
        derived_env=lambda cfg: {"SESSION_MIN": str(int((cfg.get("connection") or {}).get("session_s", 60)) // 60)},
    )
    base.update(kw)
    return LaunchPolicy(**base)


# ---------------------------------------------------------------------------
# 黑名單與政策宣告
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["PATH", "path", "LD_PRELOAD", "LD_LIBRARY_PATH", "NODE_OPTIONS", "PYTHONPATH", "PYTHONSTARTUP",
     "WEBHOOK_SECURITY_MODE", "USE_FIXED_HTTP", "JWT_SECRET", "HTTPS_PROXY", "BASH_ENV", "A=B", "1BAD", ""],
)
def test_blocked_env_names(name):
    assert is_blocked_env_name(name)


@pytest.mark.parametrize("name", ["N8N_API_URL", "AUTH_TOKEN", "HERMES_GATEWAY_URL", "K8S_MCP_READ_ONLY", "LANG"])
def test_allowed_env_names(name):
    assert not is_blocked_env_name(name)


def test_policy_rejects_blocked_declarations():
    with pytest.raises(ValueError):
        _policy(env_keys=frozenset({"NODE_OPTIONS"}), editable_env_keys=frozenset())
    with pytest.raises(ValueError):
        _policy(connection_keys=frozenset({"ld_preload"}), non_env_connection_keys=frozenset())
    with pytest.raises(ValueError):
        _policy(editable_env_keys=frozenset({"NOT_IN_ENV_KEYS"}))


def test_required_connection_lists_blank_fields_in_declared_order():
    """子行程缺了就起不來的連線欄位（閘道的佔位 MCP 與 /healthz 用，見 setup_status.py）。"""
    policy = _policy(required_connection=(("app_url", "App 網址"), ("app_key", "API key")))
    assert policy.missing_connection(None) == [("app_url", "App 網址"), ("app_key", "API key")]
    assert policy.missing_connection({"app_url": "  ", "app_key": "k"}) == [("app_url", "App 網址")]
    assert policy.missing_connection({"app_key": None, "app_url": "https://x"}) == [("app_key", "API key")]
    assert policy.missing_connection({"app_url": "https://x", "app_key": "k"}) == []
    assert _policy().missing_connection({}) == []  # 沒宣告就什麼都不缺


@pytest.mark.parametrize(
    "required",
    [
        (("not_a_key", "x"),),  # 不是 connection 鍵
        (("session_s", "Session"),),  # 只存不注入子行程的鍵
        (("app_url", ""),),  # 沒有標籤
        (("app_url", "a"), ("app_url", "b")),  # 重複
        ("app_url",),  # 不是 (鍵, 標籤)
        [("app_url", "App 網址")],  # 要 tuple（政策是 frozen dataclass，要能 hash）
    ],
)
def test_required_connection_declaration_is_validated(required):
    with pytest.raises(ValueError):
        _policy(required_connection=required)


def test_backend_connection_lists_blank_fields_in_declared_order():
    """後端連線欄位（子行程起得來、沒填就沒接上後端；/healthz 的 backend 用，見 setup_status.health_fields）。"""
    policy = _policy(backend_connection=(("app_url", "App 網址"), ("app_key", "API key")))
    assert policy.missing_backend_connection(None) == [("app_url", "App 網址"), ("app_key", "API key")]
    assert policy.missing_backend_connection({}) == [("app_url", "App 網址"), ("app_key", "API key")]
    assert policy.missing_backend_connection({"app_url": "  ", "app_key": "k"}) == [("app_url", "App 網址")]
    assert policy.missing_backend_connection({"app_key": None, "app_url": "https://x"}) == [("app_key", "API key")]
    assert policy.missing_backend_connection({"app_url": "https://x", "app_key": "k"}) == []
    assert policy.missing_connection({}) == []  # 後端欄位不是必要欄位：不影響 required_connection 的判斷
    assert _policy().missing_backend_connection({}) == []  # 沒宣告就什麼都不缺


@pytest.mark.parametrize(
    "backend",
    [
        (("not_a_key", "x"),),  # 不是 connection 鍵
        (("session_s", "Session"),),  # 只存不注入子行程的鍵
        (("app_url", ""),),  # 沒有標籤
        (("app_url", "  "),),  # 標籤只有空白
        ((" ", "App 網址"),),  # 鍵只有空白
        (("app_url", "a"), ("app_url", "b")),  # 重複
        ("app_url",),  # 不是 (鍵, 標籤)
        (("app_url", "App 網址", "x"),),  # 不是兩個一組
        [("app_url", "App 網址")],  # 要 tuple（政策是 frozen dataclass，要能 hash）
    ],
)
def test_backend_connection_declaration_is_validated(backend):
    """宣告時的驗證與 required_connection 相同。"""
    with pytest.raises(ValueError):
        _policy(backend_connection=backend)


def test_a_key_is_either_required_or_backend_not_both():
    """缺了起不來的欄位放 required_connection（/healthz 的 mcp 報 not_configured），沒接上後端才放 backend_connection。"""
    with pytest.raises(ValueError):
        _policy(required_connection=(("app_url", "App 網址"),), backend_connection=(("app_url", "App 網址"),))
    both = _policy(required_connection=(("app_url", "App 網址"),), backend_connection=(("app_key", "API key"),))
    assert both.missing_connection({}) == [("app_url", "App 網址")]
    assert both.missing_backend_connection({}) == [("app_key", "API key")]
    hash(both)  # 政策是 frozen dataclass：宣告要能 hash


# ---------------------------------------------------------------------------
# 子行程環境變數
# ---------------------------------------------------------------------------


def test_env_with_policy_only_injects_allowlisted(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "admin-jwt")
    policy = _policy()
    cfg = {"connection": {"app_url": "https://x", "app_key": "k", "session_s": "600",
                          "webhook_security_mode": "permissive", "node_options": "--require /data/x.js"},
           "mcp_server": {"env": {"AUTH_TOKEN": "t", "APP_FLAG": "1", "LD_PRELOAD": "/data/x.so", "OTHER": "y"},
                          "env_prefix": "EVIL_"}}
    env = _build_subprocess_env(cfg["connection"], cfg["mcp_server"], policy, cfg)
    assert env["APP_URL"] == "https://x" and env["APP_KEY"] == "k"
    assert env["AUTH_TOKEN"] == "t" and env["APP_FLAG"] == "1"
    assert env["SESSION_MIN"] == "10"
    for name in ("WEBHOOK_SECURITY_MODE", "NODE_OPTIONS", "LD_PRELOAD", "OTHER", "SESSION_S", "EVIL_APP_URL"):
        assert name not in env
    # admin 的 JWT 簽章金鑰不傳給子行程
    assert "JWT_SECRET" not in env


def test_env_without_policy_still_blocks_dangerous_names():
    env = _build_subprocess_env(
        {"node_options": "--require x", "odoo_url": "http://o"},
        {"env": {"LD_PRELOAD": "/x.so", "PYTHONPATH": "/data", "FLAG": "1"}},
    )
    assert env["ODOO_URL"] == "http://o" and env["FLAG"] == "1"
    assert env.get("NODE_OPTIONS") != "--require x"
    assert env.get("LD_PRELOAD") != "/x.so"
    assert env.get("PYTHONPATH") != "/data"


def test_resolve_launch_uses_policy_not_config():
    cmd, args, port = resolve_launch({"command": "sh", "args": ["-c", "id"], "port": 3001}, _policy())
    assert (cmd, args, port) == ("app-mcp", ["--port", "3001"], 3001)
    # 不合法 port 退回預設，不會把垃圾塞進參數
    assert resolve_launch({"port": "abc"}, _policy())[1] == ["--port", "8000"]


async def test_process_start_runs_app_default_not_config_command(store_env, tmp_path: Path):
    marker = tmp_path / "pwned"
    out = tmp_path / "child.json"
    script = (
        "import json,os,sys,time;"
        "json.dump({'argv':sys.argv[1:],'env':{k:os.environ.get(k) for k in "
        "('APP_URL','NODE_OPTIONS','AUTH_TOKEN','SESSION_MIN','JWT_SECRET')}},open(sys.argv[1],'w'));"
        "time.sleep(30)"
    )
    set_launch_policy(_policy(command=sys.executable, args=("-c", script, str(out), "{port}")))
    await store_env.put("connection", {"app_url": "https://x", "node_options": "--require x", "session_s": 120})
    await store_env.put("mcp_server", {"command": "touch", "args": [str(marker)], "port": 8123,
                                       "env": {"AUTH_TOKEN": "t"}})
    pm = McpProcessManager()
    try:
        assert await pm.start() is True
        for _ in range(100):
            if out.exists() and out.stat().st_size:
                break
            await asyncio.sleep(0.05)
        data = json.loads(out.read_text())
        assert data["argv"] == [str(out), "8123"]
        assert data["env"] == {"APP_URL": "https://x", "NODE_OPTIONS": None, "AUTH_TOKEN": "t",
                               "SESSION_MIN": "2", "JWT_SECRET": None}
        assert not marker.exists()
        assert (await pm.status())["command"] == sys.executable
    finally:
        await pm.stop()


# ---------------------------------------------------------------------------
# 管理 API（有政策）
# ---------------------------------------------------------------------------


def _auth(c) -> dict[str, str]:
    return {"Authorization": f"Bearer {c.post('/api/auth/login', json={'password': 'admin'}).json()['token']}"}


@pytest.fixture
def policy_app(make_app, monkeypatch):
    from mcp_admin_core.routers import settings as settings_mod

    class _PM:
        is_running = True
        calls = 0

        async def start_or_restart(self):
            _PM.calls += 1
            return True

    monkeypatch.setattr(settings_mod, "get_process_manager", lambda: _PM())

    def _make(overrides):
        c = make_app(overrides)
        set_launch_policy(_policy())  # create_app(title=...) 不帶政策 → 測試自行設定
        return c, _PM

    return _make


_MCP = {"command": "app-mcp", "args": ["--port", "3000"], "port": 3000,
        "env": {"AUTH_TOKEN": "secret-token", "APP_FLAG": "0"}, "env_prefix": ""}


def test_get_settings_shows_effective_and_hides_seeded_env(policy_app):
    c, _ = policy_app({"mcp_server": {**_MCP, "command": "legacy-cmd", "args": ["--old"]}})
    body = c.get("/api/settings", headers=_auth(c)).json()
    assert body["mcp_server"]["command"] == "app-mcp"
    assert body["mcp_server"]["args"] == ["--port", "3000"]
    assert body["mcp_server"]["env"] == {"APP_FLAG": "0"}
    assert body["editable_env_keys"] == ["APP_FLAG"]
    assert "secret-token" not in json.dumps(body)
    section = c.get("/api/settings/mcp_server", headers=_auth(c)).json()
    assert section["env"] == {"APP_FLAG": "0"} and section["command"] == "app-mcp"


def test_put_mcp_server_with_policy(policy_app, tmp_config_path):
    c, pm = policy_app({"mcp_server": dict(_MCP)})
    h = _auth(c)
    # SettingsPage 送回整包（顯示值）＋改可編輯開關 → OK
    r = c.put("/api/settings/mcp_server",
              json={"command": "app-mcp", "args": ["--port", "3000"], "port": 3000, "env": {"APP_FLAG": "1"}},
              headers=revision_headers(c, h, read_path='/api/settings'))
    assert r.status_code == 200, r.text
    stored = json.loads(tmp_config_path.read_text())["mcp_server"]
    assert stored["env"] == {"AUTH_TOKEN": "secret-token", "APP_FLAG": "1"}
    assert pm.calls == 1
    # 改指令、改 seed 的 env、加任意 env → 400
    for body in ({"command": "/bin/sh"}, {"args": ["-c", "id"]}, {"env": {"AUTH_TOKEN": "mine"}},
                 {"env": {"NEW_VAR": "x"}}, {"env": {"NODE_OPTIONS": "--require /data/x.js"}}):
        assert c.put("/api/settings/mcp_server", json=body, headers=revision_headers(c, h, read_path='/api/settings')).status_code == 400, body
    assert json.loads(tmp_config_path.read_text())["mcp_server"]["env"] == {"AUTH_TOKEN": "secret-token",
                                                                            "APP_FLAG": "1"}


def test_put_connection_with_policy_rejects_unknown_keys(policy_app, tmp_config_path):
    c, _ = policy_app({"connection": {"app_url": "https://x"}})
    h = _auth(c)
    r = c.put("/api/settings/connection", json={"webhook_security_mode": "permissive"}, headers=revision_headers(c, h, read_path='/api/settings'))
    assert r.status_code == 400
    r = c.put("/api/settings/connection", json={"app_url": "https://y"}, headers=revision_headers(c, h, read_path='/api/settings'))
    assert r.status_code == 200
    assert json.loads(tmp_config_path.read_text())["connection"] == {"app_url": "https://y"}


# ---------------------------------------------------------------------------
# 各 app 的政策：與 PaaS 範本 seed 一致、連線頁寫入的鍵都在白名單內
# ---------------------------------------------------------------------------


def _policies():
    from hermes_mcp_admin.main import LAUNCH_POLICY as hermes
    from k8s_mcp_admin.main import LAUNCH_POLICY as k8s
    from litellm_mcp_admin.main import LAUNCH_POLICY as litellm
    from n8n_mcp_admin.main import LAUNCH_POLICY as n8n
    from nextcloud_mcp_admin.main import LAUNCH_POLICY as nextcloud
    from odoo_mcp_admin.main import LAUNCH_POLICY as odoo
    from opendesign_mcp_admin.main import LAUNCH_POLICY as opendesign
    from vk_mcp_admin.main import LAUNCH_POLICY as vk

    return dict(hermes=hermes, k8s=k8s, litellm=litellm, n8n=n8n, nextcloud=nextcloud,
                odoo=odoo, opendesign=opendesign, vk=vk)


# woow_paas_platform src/data/cloud_app_templates.xml 的 mcp_server seed（opendesign 是 #1369 原生 launcher）。
# 已封存的 odoo-manage（隱藏範本固定在舊映像 d5032a72）自 #78 起移出 uv workspace，不再比對。
_TEMPLATE_SEEDS = {
    "hermes": (8000, ["hermes-mcp-server", "--host", "127.0.0.1", "--port", "8000"]),
    "odoo": (8000, ["woow-odoo-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8000"]),
    "n8n": (3000, ["n8n-mcp-http", "--port", "3000"]),
    "k8s": (8000, ["k8s-mcp-http", "--port", "8000"]),
    "vk": (8000, ["supergateway", "--stdio", "vibe-kanban-mcp --mode global", "--port", "8000",
                  "--outputTransport", "streamableHttp", "--healthEndpoint", "/healthz"]),
    "opendesign": (8000, ["opendesign-mcp-server", "--host", "127.0.0.1", "--port", "8000"]),
    "litellm": (8000, ["litellm-mcp-server", "--port", "8000"]),
    # nextcloud 的 PaaS 範本尚未上架；此為 apps/nextcloud/README.md 與 seed_defaults 的 seed 契約。
    "nextcloud": (8000, ["nextcloud-mcp-server", "--port", "8000"]),
}


def test_app_policies_match_template_seeds():
    policies = _policies()
    assert set(policies) == set(_TEMPLATE_SEEDS)
    for name, (port, expected) in _TEMPLATE_SEEDS.items():
        assert policies[name].build_command(port) == expected, name


def test_only_nextcloud_declares_required_connection():
    """只有 Nextcloud 的子行程缺連線就結束（exit 2）；n8n（文件模式）、odoo、litellm、hermes…沒有連線也照樣啟動，
    宣告了反而會把別的原因（當掉、重啟中）誤報成缺設定——它們改宣告 backend_connection（見 test_backend_connection_per_product）。
    k8s 是「kubeconfig 或 in-cluster」的條件式需求，不宣告。"""
    policies = _policies()
    assert policies["nextcloud"].required_connection == (
        ("base_url", "Nextcloud 網址"), ("username", "Nextcloud 帳號"), ("app_password", "App password"))
    assert {name for name, policy in policies.items() if policy.required_connection} == {"nextcloud"}


# 第五輪（負責人 2026-10-27）：子行程沒有連線也起得來、但等於沒接上後端的產品宣告 backend_connection（/healthz 的 backend）。
# 每個鍵＝沒填就「工具呼叫失敗或只剩部分功能」：odoo 上游四個 ODOO_* 都在才建連線、XML-RPC 要資料庫＋帳號＋密碼；litellm 沒網址
# 連 localhost:4000、沒金鑰 LiteLLM 回 401；hermes 10 支工具要 Dashboard 登入、chat 要 Gateway＋API Key；n8n 上游兩個都有才開
# 21 支管理工具（否則 7 支文件工具）；vk 沒有 VIBE_BACKEND_URL 時 vibe-kanban-mcp 找不到 VK Host 就結束；opendesign 沒有 daemon
# 網址。不宣告：nextcloud（三個欄位都是 required_connection）、k8s（沒有 kubeconfig 上游就起不來，屬於「必要」而且是
# 「kubeconfig 或 in-cluster」的條件式需求）。選填欄位（n8n 的 Native MCP token、opendesign 的 Basic Auth／Chat Timeout）不算。
_BACKEND_CONNECTION = {
    "odoo": ("odoo_url", "odoo_db", "odoo_username", "odoo_password"),
    "litellm": ("base_url", "master_key"),
    "hermes": ("gateway_url", "gateway_api_key", "dashboard_url", "dashboard_username", "dashboard_password"),
    "n8n": ("n8n_api_url", "n8n_api_key"),
    "vk": ("vibe_backend_url",),
    "opendesign": ("od_api_base",),
    "nextcloud": (),
    "k8s": (),
}


def test_backend_connection_per_product():
    policies = _policies()
    assert set(policies) == set(_BACKEND_CONNECTION)
    for name, keys in _BACKEND_CONNECTION.items():
        assert tuple(key for key, _label in policies[name].backend_connection) == keys, name


_FRONTEND = Path(__file__).resolve().parents[3] / "apps"
_FIELD_LABEL = re.compile(r"\{ name: '(\w+)', label: '([^']+)'")


@pytest.mark.parametrize("name", [name for name, keys in _BACKEND_CONNECTION.items() if keys])
def test_backend_connection_labels_are_the_connection_page_labels(name):
    """標籤與 Connection 頁（frontend/src/appConfig.jsx）相同，也都是頁面上有的欄位。"""
    labels = dict(_FIELD_LABEL.findall((_FRONTEND / name / "frontend" / "src" / "appConfig.jsx").read_text(encoding="utf-8")))
    declared = _policies()[name].backend_connection
    assert declared == tuple((key, labels[key]) for key, _label in declared), name


def test_app_connection_forms_within_allowlist():
    from hermes_mcp_admin.routers.config import DualConnectionUpdate
    from k8s_mcp_admin.routers.config import ConnectionUpdateRequest as K8sReq
    from litellm_mcp_admin.routers.config import ConnectionUpdateRequest as LitellmReq
    from n8n_mcp_admin.routers.config import ConnectionUpdate as N8nReq
    from nextcloud_mcp_admin.routers.config import ConnectionUpdateRequest as NextcloudReq
    from odoo_mcp_admin.routers.config import ConnectionUpdateRequest as OdooReq
    from opendesign_mcp_admin.routers.config import ConnectionUpdateRequest as OdReq
    from vk_mcp_admin.routers.config import ConnectionUpdateRequest as VkReq

    policies = _policies()
    forms = {
        "hermes": set(DualConnectionUpdate.CONNECTION_FIELDS),
        "k8s": set(K8sReq.model_fields),
        "litellm": set(LitellmReq.model_fields),
        "n8n": set(N8nReq.model_fields),
        "nextcloud": set(NextcloudReq.model_fields),
        "odoo": set(OdooReq.model_fields),
        "opendesign": set(OdReq.model_fields),
        "vk": set(VkReq.model_fields),
    }
    for name, fields in forms.items():
        fields.discard("restart")
        assert fields <= policies[name].connection_keys, (name, fields - policies[name].connection_keys)
