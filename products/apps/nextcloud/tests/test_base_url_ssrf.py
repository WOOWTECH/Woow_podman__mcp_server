"""base_url 的 SSRF 檢查：存檔、測試連線、健康探測都過核心既有的檢查（與 n8n、EMQX 同一套；2026-11-14 實測 F-1）。

修正前（映像沒有 ``backend_policy``、``LAUNCH_POLICY`` 沒宣告 ``url_connection_keys``、router 沒呼叫 ``refuse_blocked_urls``）：

- ``PUT /api/config/connection {"base_url": "http://169.254.169.254", …}`` 回 200 並存檔；``127.0.0.1:8080``、``localhost:1``、
  ``[::1]``、``10.43.0.1``、叢集內 Service 都一樣；
- ``POST /api/config/test`` 真的連過去，用訊息區分「埠關閉」與「有 HTTP 服務」，還帶出目標對
  ``GET /ocs/v2.php/cloud/user`` 回的狀態碼——租戶可以拿它當探測叢集內網的跳板。

現在 ``base_url`` 與 n8n／EMQX 一樣用 ``mcp_admin_core.util.ssrf.check_url``（模式讀部署端的 ``WEBHOOK_SECURITY_MODE``，
預設 strict）：存檔回 400 ``detail.code == "ssrf_blocked"``、不存檔；測試連線與 ``/api/health`` 不送出任何請求。

不連任何外部網路：主機名由 conftest 的 ``fake_dns`` 解析（測試替身），其餘用 IP 字面值、httpx.MockTransport，或只綁
127.0.0.1 的計數用假伺服器。
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest
from management_client import revision_headers
from mcp_admin_core.connection_apply import SSRF_BLOCKED
from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

from nextcloud_mcp_admin import health_probes, log_redaction
from nextcloud_mcp_admin.main import LAUNCH_POLICY
from nextcloud_mcp_admin.routers import config as config_router

from .conftest import CONN, PUBLIC_TEST_ADDRESS, stored_config
from .fake_nextcloud import BASE_URL, LOGIN, PASSWORD

SAVED_URL = "https://keep.example.com/nc"
STORED = {"base_url": SAVED_URL, "username": LOGIN, "app_password": PASSWORD}


class _FakePM:
    def __init__(self) -> None:
        self.is_running = False
        self.calls = 0

    async def start_or_restart(self):
        self.calls += 1
        return True


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    """版本與探測結果的快取是行程共用的：這個檔案的假後端回 401／{}，不可把「沒有版本」留給後面的測試。"""
    monkeypatch.setattr(health_probes, "_version_cache", {})
    monkeypatch.setattr(health_probes, "_last_probe", None)


@pytest.fixture(autouse=True)
def _clear_secrets():
    """成功的存檔會登記 app password 供 log 遮罩（全域狀態）；每個測試前後清掉。"""
    log_redaction.set_secrets(None)
    yield
    log_redaction.set_secrets(None)


@pytest.fixture
def fake_pm(monkeypatch):
    pm = _FakePM()
    monkeypatch.setattr(config_router, "get_process_manager", lambda: pm)
    return pm


@pytest.fixture
def launch_policy():
    """別的測試以 create_app() 組 app 時會把全域政策換掉；共用的 /api/settings 路徑要明確指定產品的政策。"""
    previous = get_launch_policy()
    set_launch_policy(LAUNCH_POLICY)
    yield LAUNCH_POLICY
    set_launch_policy(previous)


def put(client, headers=None, **body):
    payload = {"base_url": SAVED_URL, "username": LOGIN, "app_password": "", "restart": False, **body}
    return client.put("/api/config/connection", json=payload,
                      headers=revision_headers(client, headers, read_path="/api/config"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CountingBackend:
    """只綁 127.0.0.1 的假伺服器：不管收到什麼都回 200 ``{}``，只記有幾個請求打到它（SSRF 擋下時應該是 0 個）。"""

    def __init__(self) -> None:
        self.hits: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _any(self) -> None:
                outer.hits.append((self.command, self.path))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_PROPFIND = do_PUT = do_DELETE = do_REPORT = _any

            def log_message(self, *args) -> None:  # noqa: D401 — 測試不印存取 log
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> CountingBackend:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# 存檔：PUT /api/config/connection
# ---------------------------------------------------------------------------

# (網址, 核心的拒絕原因)。原因文字取自核心的 SSRF 規則（packages/mcp-admin-core/tests/test_ssrf.py 逐案比對上游 n8n-mcp）。
BLOCKED = [
    ("http://169.254.169.254", "Cloud metadata endpoint blocked"),
    ("http://169.254.169.254/latest/meta-data", "Cloud metadata endpoint blocked"),
    ("http://100.100.100.200", "Cloud metadata endpoint blocked"),
    ("http://metadata.google.internal", "Cloud metadata endpoint blocked"),
    ("http://127.0.0.1:8080", "Localhost access is blocked"),
    ("http://localhost:1", "Localhost access is blocked"),
    ("http://[::1]", "Localhost access is blocked"),
    ("http://0.0.0.0", "Localhost access is blocked"),
    ("http://nextcloud.localhost", "Localhost access is blocked"),
    ("http://10.43.0.1", "Private IP addresses not allowed"),
    ("http://172.16.0.5:8080/nextcloud", "Private IP addresses not allowed"),
    ("http://192.168.1.10", "Private IP addresses not allowed"),
    ("http://100.64.0.1", "Private IP addresses not allowed"),
    ("http://[fd00::1]", "IPv6 private address not allowed"),
    ("http://[::ffff:10.0.0.1]", "IPv6 private/mapped address not allowed"),
    # 主機名：解析出的任何一個位址是私網／metadata／loopback 就擋（fake_dns 登記的位址，見 resolver_zoo）
    ("http://nextcloud.default.svc.cluster.local", "Hostname resolves to a blocked address (Private IP addresses not allowed)"),
    ("https://split.example.com", "Hostname resolves to a blocked address (Private IP addresses not allowed)"),
    ("https://meta.example.com", "Hostname resolves to a blocked address (Cloud metadata endpoint blocked)"),
    ("https://loop.example.com", "Localhost access is blocked"),
]


@pytest.fixture
def resolver_zoo(fake_dns):
    """叢集內 Service 名稱解析成私網位址、同一個名稱同時有公開與私網位址（split-horizon）、解析到 metadata／loopback。"""
    fake_dns.add("nextcloud.default.svc.cluster.local", "10.43.17.5")
    fake_dns.add("split.example.com", PUBLIC_TEST_ADDRESS, "10.0.0.8")
    fake_dns.add("meta.example.com", "169.254.169.254")
    fake_dns.add("loop.example.com", "127.0.0.1")
    return fake_dns


@pytest.mark.parametrize(("url", "reason"), BLOCKED, ids=[url for url, _ in BLOCKED])
def test_save_refuses_urls_the_core_refuses_and_keeps_the_config(make_client, config_path, fake_pm, resolver_zoo, url, reason):
    client = make_client(connection=dict(STORED))
    before = config_path.read_bytes()
    reply = put(client, base_url=url, app_password="Brand-New-App-Pass")
    assert reply.status_code == 400, reply.text
    detail = reply.json()["detail"]
    assert detail["code"] == SSRF_BLOCKED and detail["field"] == "base_url" and detail["reason"] == reason
    assert reason in detail["message"] and "未存檔" in detail["message"]
    # 不外洩目標內容：回應不帶網址、主機名、位址，也不帶送出的密碼
    host = urlsplit(url).hostname
    for leaked in (host, url, "Brand-New-App-Pass", "10.43.17.5", "10.0.0.8"):
        assert leaked not in reply.text
    assert config_path.read_bytes() == before  # 設定檔一個位元組都沒變（新的 app password 也沒存）
    assert fake_pm.calls == 0  # 沒有套用到子行程


@pytest.mark.parametrize("restart", [True, False])
def test_save_refuses_before_any_effect_whatever_restart_says(make_client, config_path, fake_pm, restart):
    client = make_client(connection=dict(STORED))
    reply = put(client, base_url="http://10.43.0.1", restart=restart)
    assert reply.status_code == 400 and reply.json()["detail"]["code"] == SSRF_BLOCKED
    assert stored_config(config_path)["connection"] == STORED and fake_pm.calls == 0


# 合法的公開網址不可被誤擋：帶埠、帶路徑、https／http、IDN 主機名、公開 IPv4／IPv6 字面值（主機名由 fake_dns 解析成公開位址）
PUBLIC = [
    ("https://cloud.example.com", "https://cloud.example.com"),
    ("  https://cloud.example.com/  ", "https://cloud.example.com"),
    ("https://cloud.example.com:8443", "https://cloud.example.com:8443"),
    ("https://example.com/nextcloud", "https://example.com/nextcloud"),
    ("https://example.com:8443/nextcloud/", "https://example.com:8443/nextcloud"),
    ("https://example.com/nextcloud/index.php/apps/files/", "https://example.com/nextcloud"),  # 既有的尾段切除照舊
    ("http://cloud.example.com", "http://cloud.example.com"),
    ("https://雲端.example.com/nc", "https://雲端.example.com/nc"),
    ("https://xn--fiq228c.example.com", "https://xn--fiq228c.example.com"),
    ("https://203.0.113.20/nextcloud", "https://203.0.113.20/nextcloud"),
    ("https://[2606:4700::1111]/", "https://[2606:4700::1111]"),
    ("HTTPS://Cloud.Example.com", "https://Cloud.Example.com"),
]


@pytest.mark.parametrize(("url", "saved"), PUBLIC, ids=[url.strip() for url, _ in PUBLIC])
def test_save_accepts_legitimate_public_urls(make_client, config_path, fake_pm, url, saved):
    client = make_client(connection=dict(STORED))
    reply = put(client, base_url=url)
    assert reply.status_code == 200, reply.text
    assert stored_config(config_path)["connection"]["base_url"].lower() == saved.lower()


def test_save_checks_each_hostname_through_the_resolver_only(make_client, fake_pm, fake_dns):
    """主機名要解析（DNS 由測試替身回答）；IP 字面值不查 DNS。"""
    client = make_client(connection=dict(STORED))
    assert put(client, base_url="https://cloud.example.com").status_code == 200
    assert put(client, base_url="https://203.0.113.20").status_code == 200
    assert fake_dns.lookups == ["cloud.example.com"]


def test_save_refuses_a_name_that_does_not_resolve(make_client, config_path, fake_pm, fake_dns, monkeypatch):
    async def nothing(host, port, **_):
        raise OSError("Name or service not known")

    monkeypatch.setattr(fake_dns, "getaddrinfo", nothing)
    client = make_client(connection=dict(STORED))
    reply = put(client, base_url="https://typo.example.com")
    assert reply.status_code == 400 and reply.json()["detail"]["reason"] == "DNS resolution failed"
    assert stored_config(config_path)["connection"] == STORED


def test_clearing_and_unconfigured_accounts_are_not_checked(make_client, config_path, fake_pm, fake_dns):
    """空的 base_url（尚未設定、有意清空）不檢查、不查 DNS；只改帳號與密碼不動網址。"""
    client = make_client(connection={})  # 新服務：還沒有任何連線設定
    first = put(client, base_url="", username=LOGIN, app_password=PASSWORD)
    assert first.status_code == 200, first.text
    assert stored_config(config_path)["connection"] == {"base_url": "", "username": LOGIN, "app_password": PASSWORD}
    assert put(client, base_url="https://cloud.example.com").status_code == 200
    assert put(client, base_url="").status_code == 200  # 清空
    assert stored_config(config_path)["connection"]["base_url"] == ""
    assert fake_dns.lookups == ["cloud.example.com"]  # 兩次空值都沒有查 DNS


def test_save_still_rejects_malformed_urls_before_the_ssrf_check(make_client, config_path, fake_pm, fake_dns):
    client = make_client(connection=dict(STORED))
    for bad in ("https://canary-user:canary-pw@cloud.example.com", "https://cloud.example.com/?x=1", "cloud.example.com",
                "https://cloud.example.com:99999"):
        reply = put(client, base_url=bad)
        assert reply.status_code == 400 and not isinstance(reply.json()["detail"], dict), (bad, reply.text)
        assert "canary" not in reply.text
    assert stored_config(config_path)["connection"] == STORED and fake_dns.lookups == []


def test_a_check_that_blows_up_is_a_400_not_a_500(make_client, config_path, fake_pm, monkeypatch):
    from mcp_admin_core.util import ssrf

    async def boom(url, mode=None):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(ssrf, "check_url", boom)
    client = make_client(connection=dict(STORED))
    reply = put(client, base_url="https://203.0.113.40")
    assert reply.status_code == 400
    assert reply.json()["detail"]["code"] == SSRF_BLOCKED and reply.json()["detail"]["reason"] == "Invalid URL format"
    assert stored_config(config_path)["connection"] == STORED


# 模式讀部署端的 WEBHOOK_SECURITY_MODE（租戶改不了）：strict 預設；moderate 放行 localhost；permissive 只擋 metadata
MODE_CASES = [
    # (模式, 網址, 是否通過)
    ("strict", "http://127.0.0.1:8080", False),
    ("moderate", "http://127.0.0.1:8080", True),
    ("moderate", "http://localhost:8080", True),
    ("moderate", "http://[::1]:8080", True),
    ("moderate", "http://10.43.0.1", False),
    ("moderate", "http://169.254.169.254", False),
    ("permissive", "http://127.0.0.1:8080", True),
    ("permissive", "http://10.43.0.1", True),
    ("permissive", "http://192.168.1.10:8080", True),
    ("permissive", "http://169.254.169.254", False),
    ("permissive", "http://[fd00:ec2::254]", False),  # AWS IPv6 metadata：所有模式都擋
    ("permissive", "http://[::ffff:0:169.254.169.254]", False),  # 內嵌的 IPv4 metadata：所有模式都擋
    ("unknown-mode", "http://127.0.0.1:8080", False),  # 不認得的值＝strict
]


@pytest.mark.parametrize(("mode", "url", "allowed"), MODE_CASES)
def test_modes_follow_the_core_documentation(make_client, config_path, fake_pm, monkeypatch, mode, url, allowed):
    monkeypatch.setenv("WEBHOOK_SECURITY_MODE", mode)
    client = make_client(connection=dict(STORED))
    reply = put(client, base_url=url)
    if allowed:
        assert reply.status_code == 200, reply.text
        assert stored_config(config_path)["connection"]["base_url"] == url
    else:
        assert reply.status_code == 400 and reply.json()["detail"]["code"] == SSRF_BLOCKED
        assert stored_config(config_path)["connection"] == STORED


# ---------------------------------------------------------------------------
# 共用路徑與額外帳號：LaunchPolicy.url_connection_keys
# ---------------------------------------------------------------------------


def test_launch_policy_declares_base_url_as_the_url_field():
    from mcp_admin_core.launch_policy import LaunchPolicy

    assert config_router.URL_FIELDS == frozenset({"base_url"})
    assert LAUNCH_POLICY.url_connection_keys == config_router.URL_FIELDS
    assert LAUNCH_POLICY.url_connection_keys <= LAUNCH_POLICY.connection_keys
    with pytest.raises(ValueError, match="url_connection_keys"):  # 核心會擋「不是連線鍵的網址欄位」
        LaunchPolicy(command="x", connection_keys=frozenset({"a"}), url_connection_keys=frozenset({"b"}))


def test_shared_settings_connection_path_is_guarded_too(make_client, config_path, fake_pm, launch_policy, monkeypatch):
    """共用的 PUT /api/settings/connection 不能拿來繞過產品自己的 Connection 頁。"""
    monkeypatch.setattr("mcp_admin_core.routers.settings.get_process_manager", lambda: fake_pm)
    client = make_client(connection=dict(STORED), app="main")
    for url in ("http://169.254.169.254", "http://127.0.0.1:8080", "http://10.43.0.1", "http://localhost:1"):
        reply = client.put("/api/settings/connection", json={"base_url": url},
                           headers=revision_headers(client, None, read_path="/api/settings"))
        assert reply.status_code == 400 and reply.json()["detail"]["code"] == SSRF_BLOCKED, (url, reply.text)
        assert reply.json()["detail"]["field"] == "base_url"
        assert stored_config(config_path)["connection"] == STORED
    ok = client.put("/api/settings/connection", json={"base_url": "https://cloud.example.com/nc"},
                    headers=revision_headers(client, None, read_path="/api/settings"))
    assert ok.status_code == 200, ok.text
    assert stored_config(config_path)["connection"]["base_url"] == "https://cloud.example.com/nc"


def test_extra_accounts_go_through_the_same_check(make_client, config_path, fake_pm, launch_policy, monkeypatch):
    """額外帳號（X-MCP-Profile）的連線設定走同一條路：同樣被擋、不寫進那個帳號的設定檔，公開網址照常通過。"""
    monkeypatch.setattr("mcp_admin_core.profiles._memory_limit_mib", lambda: 1024)  # 不依賴測試機器的 cgroup
    client = make_client(connection=dict(STORED), app="main")
    created = client.post("/api/profiles", json={"label": "第二個 Nextcloud"})
    assert created.status_code == 201, created.text
    header = {"X-MCP-Profile": created.json()["id"]}
    profile_file = config_path.parent / "profiles" / created.json()["id"] / "config.json"
    before = profile_file.read_bytes()

    for url in ("http://169.254.169.254", "http://127.0.0.1:8080", "http://10.43.0.1"):
        reply = put(client, header, base_url=url)
        assert reply.status_code == 400 and reply.json()["detail"]["code"] == SSRF_BLOCKED, (url, reply.text)
    shared = client.put("/api/settings/connection", json={"base_url": "http://10.43.0.1"},
                        headers=revision_headers(client, header, read_path="/api/settings"))
    assert shared.status_code == 400 and shared.json()["detail"]["code"] == SSRF_BLOCKED
    assert profile_file.read_bytes() == before  # 那個帳號的設定檔沒變
    assert stored_config(config_path)["connection"] == STORED  # 預設帳號也沒變

    try:
        ok = put(client, header, base_url="https://cloud2.example.com", username="bob", app_password="Second-App-Pass-1")
        assert ok.status_code == 200, ok.text
        assert json.loads(profile_file.read_text())["connection"]["base_url"] == "https://cloud2.example.com"
        assert stored_config(config_path)["connection"] == STORED
    finally:
        log_redaction.remove_profile_secrets(created.json()["id"])


# ---------------------------------------------------------------------------
# 測試連線與健康探測：被擋的網址一個請求都不送（修正前是探測跳板）
# ---------------------------------------------------------------------------


def _recording_transport():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    return seen, httpx.MockTransport(handler)


@pytest.mark.parametrize("url", [u for u, _ in BLOCKED])
async def test_check_nextcloud_sends_nothing_to_a_blocked_address(url, resolver_zoo):
    seen, transport = _recording_transport()
    result = await health_probes.check_nextcloud(url, LOGIN, PASSWORD, transport=transport)
    assert seen == []
    assert result["healthy"] is False and result["auth_ok"] is None and result["version"] is None
    assert result["error"].startswith("Blocked by SSRF protection: ")
    assert PASSWORD not in repr(result) and urlsplit(url).hostname not in result["error"]


async def test_check_nextcloud_still_reaches_a_public_address():
    seen, transport = _recording_transport()
    result = await health_probes.check_nextcloud(BASE_URL, LOGIN, PASSWORD, transport=transport)
    assert seen and seen[0].url.host == "cloud.example.com"  # 沒被誤擋：請求真的送出（假的 401 在這裡不重要）
    assert result["auth_ok"] is False


async def test_check_nextcloud_keeps_reporting_malformed_settings_first():
    """帶帳密、壞 scheme 之類的舊設定：照舊先報「設定不正確」（不是 SSRF 訊息），也不送請求。"""
    seen, transport = _recording_transport()
    for bad in ("ftp://x", "https://canary-user:canary-pw@cloud.example.com"):
        result = await health_probes.check_nextcloud(bad, LOGIN, PASSWORD, transport=transport)
        assert result["healthy"] is False and "設定不正確" in result["error"] and "canary" not in result["error"]
    assert seen == []


def test_test_connection_sends_nothing_and_answers_the_same_for_a_closed_port_and_a_live_service(make_client, fake_pm):
    """修正前的探測差異：埠關閉與「有 HTTP 服務」回不同訊息（還帶目標回的狀態碼）。現在兩者完全相同、一個請求都不送。"""
    answers = []
    with CountingBackend() as live:
        for url in (live.url, f"http://127.0.0.1:{free_port()}", "http://10.43.0.1", "http://169.254.169.254"):
            client = make_client(connection={"base_url": url, "username": LOGIN, "app_password": PASSWORD})
            reply = client.post("/api/config/test")
            assert reply.status_code == 200, reply.text
            body = reply.json()
            assert body["success"] is False and "Blocked by SSRF protection" in body["message"]
            assert PASSWORD not in reply.text
            assert body["details"]["auth_ok"] is None and body["details"]["version"] is None
            answers.append(body["message"].split("；MCP 子行程")[0])
        assert live.hits == []  # 真的有 HTTP 服務在聽的 loopback 位址，也沒有收到任何連線請求
    # 兩個 loopback 位址（一個有服務、一個埠關閉）的回答逐字相同
    assert answers[0] == answers[1]


def test_test_connection_follows_the_mode_and_does_reach_loopback_in_moderate(make_client, fake_pm, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECURITY_MODE", "moderate")
    with CountingBackend() as live:
        client = make_client(connection={"base_url": live.url, "username": LOGIN, "app_password": PASSWORD})
        body = client.post("/api/config/test").json()
        assert "Blocked by SSRF protection" not in body["message"]
        assert live.hits and live.hits[0][0] == "GET" and "/ocs/v2.php/cloud/user" in live.hits[0][1]
    # moderate 仍擋私網與 metadata
    for url in ("http://10.43.0.1", "http://169.254.169.254"):
        client = make_client(connection={"base_url": url, "username": LOGIN, "app_password": PASSWORD})
        assert "Blocked by SSRF protection" in client.post("/api/config/test").json()["message"]


def test_test_connection_with_an_unset_url_is_unchanged(make_client, fake_pm, fake_dns):
    body = make_client(connection={}).post("/api/config/test").json()
    assert body["success"] is False and "base_url" in body["message"] and "SSRF" not in body["message"]
    assert fake_dns.lookups == []


def test_health_endpoint_does_not_probe_a_blocked_address(make_client, fake_pm, monkeypatch):
    """GET /api/health（儀表板）也會呼叫探測：舊設定裡存下的私網網址，同樣不連。"""
    monkeypatch.setattr(health_probes, "_last_probe", None)
    with CountingBackend() as live:
        client = make_client(connection={"base_url": live.url, "username": LOGIN, "app_password": PASSWORD}, app="main")
        body = client.get("/api/health").json()
        assert live.hits == []
    assert body["target_app"]["healthy"] is False
    assert body["target_app"]["error"].startswith("Blocked by SSRF protection: ")
    assert PASSWORD not in json.dumps(body)


def test_connection_forms_keep_working_end_to_end(make_client, config_path, fake_pm, authed, monkeypatch):
    """存一個公開網址，再按測試連線（以 fake Nextcloud 當後端）：存檔與測試都沒被誤擋。"""
    real = health_probes.check_nextcloud

    async def with_transport(base, user, password, **_):
        return await real(base, user, password, transport=authed.transport)

    monkeypatch.setattr(health_probes, "check_nextcloud", with_transport)
    client = make_client(connection={})
    assert put(client, **CONN).status_code == 200
    body = client.post("/api/config/test").json()
    assert body["success"] is True, body
    assert body["details"]["url"] == BASE_URL
