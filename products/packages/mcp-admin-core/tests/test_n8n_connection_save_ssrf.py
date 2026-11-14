"""n8n 連線存檔時就套子行程的 SSRF 規則（第二輪驗證 2026-10-26）。

過去 ``PUT /api/config/connection`` 會存下 strict 模式一定會擋的後端網址（私網、loopback、metadata），回 200、
apply_state pending，要到呼叫工具（或按 Test）才看到「SSRF protection: …」。現在存檔與子行程用同一套
``mcp_admin_core.util.ssrf.check_url``（模式讀部署端的 ``WEBHOOK_SECURITY_MODE``），被擋就回 400、不存檔；
共用的 ``PUT /api/settings/connection`` 也擋（``LaunchPolicy.url_connection_keys``）。帶帳號密碼的網址：存檔、測試
都拒絕；舊設定裡還留著的，launcher 拿掉帳密再交給 n8n-mcp。網址都用 IP 字面值，不依賴 DNS。
"""
from __future__ import annotations

from management_client import revision_headers

import json

import pytest

from mcp_admin_core.connection_apply import SSRF_BLOCKED
from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
from mcp_admin_core.util import ssrf

pytest.importorskip("n8n_mcp_admin")
from n8n_mcp_admin import launcher, tool_registry  # noqa: E402
from n8n_mcp_admin.main import LAUNCH_POLICY  # noqa: E402

SAVED_URL = "https://203.0.113.10"


class _PM:
    def __init__(self):
        self.calls = 0
        self.is_running = False

    async def start_or_restart(self):
        self.calls += 1
        return True

    async def start(self):
        self.calls += 1
        return True

    async def restart(self):
        self.calls += 1
        return True


@pytest.fixture
def n8n(tmp_config_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient
    from mcp_admin_core.routers import settings as settings_mod

    from n8n_mcp_admin.routers import config as config_mod

    monkeypatch.delenv("WEBHOOK_SECURITY_MODE", raising=False)
    full = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    full["connection"] = {"n8n_api_url": SAVED_URL, "n8n_api_key": "k"}
    tmp_config_path.write_text(json.dumps(full))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    store_mod.get_config_store()

    async def no_refresh():
        return None

    monkeypatch.setattr(tool_registry, "refresh", no_refresh)
    pm = _PM()
    monkeypatch.setattr(config_mod, "get_process_manager", lambda: pm)
    monkeypatch.setattr(settings_mod, "get_process_manager", lambda: pm)

    from n8n_mcp_admin.main import app

    set_launch_policy(LAUNCH_POLICY)
    client = TestClient(app)
    token = client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    client.pm = pm
    client.saved = lambda: json.loads(tmp_config_path.read_text())["connection"]
    return client


def save(client, url, **extra):
    return client.put("/api/config/connection", json={"n8n_api_url": url, "n8n_api_key": "", "restart": False, **extra}, headers=revision_headers(client, None, read_path='/api/config'))


@pytest.mark.parametrize(("url", "reason"), [
    ("http://10.43.5.6:5678", "Private IP addresses not allowed"),
    ("http://192.168.1.10:5678/", "Private IP addresses not allowed"),
    ("http://127.0.0.1:5678", "Localhost access is blocked"),
    # 上游不把內嵌在 IPv6 裡的 127.x 當 localhost，當 IPv6 私網擋（validateUrlSync 的同一句話）
    ("http://[::ffff:127.0.0.1]:5678", "IPv6 private/mapped address not allowed"),
    ("http://169.254.169.254/latest/meta-data", "Cloud metadata endpoint blocked"),
    ("file:///etc/passwd", "Invalid protocol. Only HTTP/HTTPS allowed."),
    ("https://user:secret@203.0.113.10", "Userinfo in URL is not allowed"),
])
def test_save_refuses_urls_the_child_would_refuse(n8n, url, reason):
    reply = save(n8n, url)
    assert reply.status_code == 400, reply.text
    detail = reply.json()["detail"]
    assert detail["code"] == SSRF_BLOCKED and detail["field"] == "n8n_api_url" and detail["reason"] == reason
    assert reason in detail["message"] and "未存檔" in detail["message"]
    assert "secret" not in reply.text  # 不回顯帳密
    assert n8n.saved()["n8n_api_url"] == SAVED_URL  # 沒有存
    assert n8n.pm.calls == 0


def test_save_accepts_public_and_empty_urls(n8n):
    assert save(n8n, " https://203.0.113.20/ ").status_code == 200
    assert n8n.saved()["n8n_api_url"] == "https://203.0.113.20/"
    assert save(n8n, "").status_code == 200  # 有意清空
    assert n8n.saved()["n8n_api_url"] == ""


def test_save_follows_the_deployment_security_mode_like_the_child(n8n, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECURITY_MODE", "permissive")
    assert save(n8n, "http://10.43.5.6:5678").status_code == 200  # 子行程 permissive 也放行私網
    assert save(n8n, "http://169.254.169.254/").json()["detail"]["reason"] == "Cloud metadata endpoint blocked"


@pytest.mark.parametrize(("url", "reason"), [
    ("http://[fd00:ec2::254]/", "Cloud metadata endpoint blocked"),       # AWS IPv6 metadata
    ("http://[fe80::a9fe:a9fe]/", "Cloud metadata endpoint blocked"),
    ("http://[::ffff:0:169.254.169.254]/", "Cloud metadata endpoint blocked"),  # translated 內嵌 metadata
    ("http://[64:ff9b:1:2::7f00:1]/", "IPv6 private/mapped address not allowed"),  # 非標準 NAT64
])
def test_permissive_mode_still_refuses_what_the_child_refuses_in_every_mode(n8n, monkeypatch, url, reason):
    """審查 F2：修正前 permissive 模式存檔與 Test connection 都放行這些位址（上游在所有模式都擋）。"""
    monkeypatch.setenv("WEBHOOK_SECURITY_MODE", "permissive")
    reply = save(n8n, url)
    assert reply.status_code == 400 and reply.json()["detail"]["reason"] == reason
    assert n8n.saved()["n8n_api_url"] == SAVED_URL


def test_generic_settings_connection_path_is_guarded_too(n8n):
    reply = n8n.put("/api/settings/connection", json={"n8n_api_url": "http://127.0.0.1:5678"}, headers=revision_headers(n8n, None, read_path='/api/settings'))
    assert reply.status_code == 400 and reply.json()["detail"]["code"] == SSRF_BLOCKED
    assert n8n.saved()["n8n_api_url"] == SAVED_URL
    assert n8n.put("/api/settings/connection", json={"n8n_api_url": "https://203.0.113.30"}, headers=revision_headers(n8n, None, read_path='/api/settings')).status_code == 200
    assert n8n.saved()["n8n_api_url"] == "https://203.0.113.30"


@pytest.mark.parametrize("url", [
    "https://example.com:99999", "https://example.com:abc", "http://n8n.example.com:5678x/",
    "https://203.0.113.10:99999",  # IP 字面值＋壞 port：修正前 200 而且存檔（子行程的 new URL() 一定拒絕）
])
def test_save_refuses_malformed_ports_with_400_not_500(n8n, url):
    reply = save(n8n, url)
    assert reply.status_code == 400, reply.text
    detail = reply.json()["detail"]
    assert (detail["code"], detail["reason"]) == (SSRF_BLOCKED, "Invalid URL format")
    reply = n8n.put("/api/settings/connection", json={"n8n_api_url": url}, headers=revision_headers(n8n, None, read_path='/api/settings'))
    assert reply.status_code == 400 and reply.json()["detail"]["reason"] == "Invalid URL format"
    assert n8n.saved()["n8n_api_url"] == SAVED_URL and n8n.pm.calls == 0


def test_a_check_that_blows_up_is_a_400_not_a_500(n8n, monkeypatch):
    async def boom(url, mode=None):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(ssrf, "check_url", boom)
    reply = save(n8n, "https://203.0.113.40")
    assert reply.status_code == 400
    assert reply.json()["detail"] | {"message": ""} == {"code": SSRF_BLOCKED, "field": "n8n_api_url",
                                                        "reason": "Invalid URL format", "message": ""}
    assert n8n.saved()["n8n_api_url"] == SAVED_URL


def test_connection_test_with_a_stored_bad_port_answers_instead_of_500(n8n, monkeypatch):
    """修正前存下的壞 port 網址（舊版沒有存檔檢查）：Test connection 回 success=false，不是 500。"""
    from n8n_mcp_admin.routers import config as config_mod

    class Stored:
        async def get(self, key, default=None):
            return {"n8n_api_url": "https://example.com:99999", "n8n_api_key": "k"} if key == "connection" else default

    monkeypatch.setattr(config_mod, "get_config_store", lambda: Stored())
    reply = n8n.post("/api/config/test")
    assert reply.status_code == 200, reply.text
    assert reply.json()["success"] is False and "Invalid URL format" in reply.json()["message"]


def test_launch_policy_declares_the_url_field():
    from n8n_mcp_admin.routers.config import URL_FIELDS

    assert LAUNCH_POLICY.url_connection_keys == URL_FIELDS == frozenset({"n8n_api_url"})
    with pytest.raises(ValueError, match="url_connection_keys"):
        LaunchPolicy(command="x", connection_keys=frozenset({"a"}), url_connection_keys=frozenset({"b"}))


USERINFO_CASES = [
    ("https://user:pw@n8n.example.com", True),
    ("https://user@n8n.example.com", True),
    ("https://:pw@n8n.example.com", True),
    ("https://n8n.example.com/path@x", False),
    ("https://n8n.example.com", False),
    ("http://169.254.169.254\\@example.com/", True),  # 反斜線解析差異：Python 視為帳密，拒絕
    ("", False),
    ("not a url", False),
]


@pytest.mark.parametrize(("url", "expected"), USERINFO_CASES)
def test_launcher_userinfo_rule_is_the_save_rule(url, expected):
    assert ssrf.has_userinfo(url) is expected
    assert launcher.has_userinfo(url) is expected
    assert (ssrf.check_url_syntax(url).reason == "Userinfo in URL is not allowed") is expected


@pytest.fixture
def launch(monkeypatch):
    monkeypatch.setattr(launcher, "resolve_entrypoint", lambda: "/x/n8n-mcp/dist/mcp/index.js")
    # main() 會先呼叫 upstream.package_dir()（未設 N8N_MCP_PACKAGE_DIR 時執行 `npm root -g`）；測試不該依賴 PATH 上有 npm。
    monkeypatch.setattr(launcher.upstream, "package_dir", lambda: "/x/n8n-mcp")
    monkeypatch.setattr(launcher, "prepare_database", lambda *args: "/data/n8n-mcp/nodes.db")
    captured = {}

    def fake_execvpe(file, argv, env):
        captured.update(env=env)
        raise SystemExit(0)

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    monkeypatch.setenv("N8N_API_KEY", "k")

    def run(url):
        monkeypatch.setenv("N8N_API_URL", url)
        with pytest.raises(SystemExit):
            launcher.main([])
        return captured["env"]

    return run


@pytest.mark.parametrize(("url", "handed"), [
    ("https://user:secret@n8n.example.com", "https://n8n.example.com"),
    ("https://user@n8n.example.com:8443/base/api/v1?x=1", "https://n8n.example.com:8443/base/api/v1?x=1"),
    ("https://:secret@[2606:4700::1111]:5678/", "https://[2606:4700::1111]:5678/"),
    ("https://us@er:sec@ret@n8n.example.com/", "https://n8n.example.com/"),  # 兩邊的解析器都以最後一個 @ 分開
])
def test_launcher_strips_userinfo_from_legacy_urls_and_warns(launch, capsys, url, handed):
    """舊設定裡還留著的帶帳密網址：拿掉帳密再交給 n8n-mcp（管理工具照常可用；上游的 health check 與錯誤訊息會帶出
    原始網址，所以不能原樣交出去），只印提醒、不含帳密。"""
    env = launch(url)
    assert env["N8N_API_URL"] == handed and env["N8N_API_KEY"] == "k"
    err = capsys.readouterr().err
    assert "userinfo" in err and "Connection" in err
    assert not any(piece in err for piece in ("secret", "sec@ret", "us@er", "user:", "user@"))  # 不回顯帳密


def test_launcher_refuses_an_authority_with_a_backslash(launch, capsys):
    """Python 與 WHATWG URL 對反斜線的解析不同（上游會把 169.254.169.254 當主機）：不改寫、不交給 n8n-mcp，印原因。"""
    env = launch("http://169.254.169.254\\@example.com/")
    assert "N8N_API_URL" not in env
    err = capsys.readouterr().err
    assert "反斜線" in err and "169.254" not in err


def test_launcher_leaves_clean_urls_alone(launch, capsys):
    assert launch("https://n8n.example.com")["N8N_API_URL"] == "https://n8n.example.com"
    assert capsys.readouterr().err == ""


def test_upstream_strips_userinfo_from_the_env_url_itself():
    """上游 2.96.2 的 env 路徑（N8nApiClient 建構子）把帳密與 #fragment 拿掉後照連：launcher 照傳不會把帳密送出去。"""
    import os

    pkg = os.environ.get("N8N_MCP_PACKAGE_DIR")
    client_js = os.path.join(pkg or "", "dist", "services", "n8n-api-client.js")
    if not pkg or not os.path.isfile(client_js):
        pytest.skip("n8n-mcp 套件不在本機（設 N8N_MCP_PACKAGE_DIR 才會跑）")
    source = open(client_js, encoding="utf-8").read()
    assert "parsed.username = '';" in source and "parsed.password = '';" in source
