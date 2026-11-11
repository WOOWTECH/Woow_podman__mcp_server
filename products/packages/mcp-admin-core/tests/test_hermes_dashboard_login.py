"""Hermes MCP 並發登入不再撞 Hermes dashboard 的登入限流（商用驗收 AI-HER-PERF-1）。

事故背景（Hermes MCP 249 → Hermes 247，0.21.6，10 個並發 MCP session × 20 次讀工具）：

- 249 的 dashboard 工具第一次用時才登入 247（``POST /auth/password-login``），cookie 快取 10 分鐘。
  快取是同一個子行程共用，但 10 個 session 同時冷啟動時全都看到快取是空的，於是同一秒打了 10 次登入。
- 247 的登入限流是「每個來源 IP 60 秒 10 次，成功也算」（``hermes_cli/dashboard_auth/routes.py``
  ``_PW_RATE_MAX_ATTEMPTS=10``、``_PW_RATE_WINDOW_SEC=60``），回 429 時不帶 ``Retry-After``。
- 249 遇到 429 直接 ``raise_for_status()``，不退避、不重試，client 拿到 isError「429 Too Many Requests」。

修正後：同一個 dashboard_url 的並發登入合併成一次（single-flight），所有 session 共用同一個 cookie；
登入回 429 時依 ``Retry-After``（上限 10 秒）或指數退避（1 秒、2 秒）重試最多 2 次。

本檔用 httpx.MockTransport 模擬一台照 247 規則限流的 dashboard；MCP 層的測試走真的 MCP 協定
（in-memory client session → FastMCP），檢查 client 實際拿到的 ``CallToolResult``。
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import time

import httpx
import pytest
from mcp.client import Client

from hermes_mcp_server import server as hermes_server

DASHBOARD = "http://hermes-agent-svc:9119"
_RealAsyncClient = httpx.AsyncClient


class FakeDashboard:
    """照 Hermes 0.21.6 規則限流的假 dashboard：每 60 秒最多 ``limit`` 次登入（成功也算），超過回 429。

    ``login_script`` 可指定前幾次登入的回應（status, headers），用完後回到限流規則。
    """

    def __init__(self, *, limit: int = 10, login_delay: float = 0.05, login_script=None):
        self.limit = limit
        self.login_delay = login_delay
        self.login_script = list(login_script or [])
        self.logins = 0           # 收到的登入請求數（含被 429 拒絕的）
        self.accepted = 0         # 計入限流窗口的次數

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/password-login":
            self.logins += 1
            await asyncio.sleep(self.login_delay)   # 讓並發的登入真的重疊
            if self.login_script:
                status, headers = self.login_script.pop(0)
                if status == 200:
                    headers = {**headers, "set-cookie": 'hermes_session_at="tok"; Path=/'}
                return httpx.Response(status, json={"detail": "scripted"}, headers=headers)
            if self.accepted >= self.limit:
                return httpx.Response(429, json={"detail": "Too many login attempts. Try again shortly."})
            self.accepted += 1
            return httpx.Response(200, json={"ok": True},
                                  headers={"set-cookie": 'hermes_session_at="tok"; Path=/'})
        assert request.headers.get("cookie") == 'hermes_session_at="tok"', "工具請求沒帶登入 cookie"
        return httpx.Response(200, json={"version": "0.21.6", "gateway_running": True,
                                         "gateway_state": "running", "active_sessions": 1})


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://hermes-agent-svc:8642")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gw-key")
    monkeypatch.setenv("HERMES_DASHBOARD_URL", DASHBOARD)
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "dash-pw")
    # 每個測試從冷快取開始（各測試有自己的 event loop，鎖也不能沿用）
    monkeypatch.setattr(hermes_server, "_cookie_cache", {})
    monkeypatch.setattr(hermes_server, "_login_locks", {}, raising=False)


@pytest.fixture
def sleeps(monkeypatch):
    """記錄退避秒數，不真的等。"""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(hermes_server, "_sleep", fake_sleep, raising=False)
    return recorded


def _install(monkeypatch, dashboard: FakeDashboard) -> None:
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(dashboard.handler)
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def _login() -> str:
    return await hermes_server._dashboard_login(DASHBOARD, "admin", "dash-pw")


# ---------------------------------------------------------------------------
# single-flight：並發冷啟動只登入一次
# ---------------------------------------------------------------------------
async def test_concurrent_cold_logins_are_coalesced_into_one(conn, monkeypatch, sleeps):
    dashboard = FakeDashboard()
    _install(monkeypatch, dashboard)

    tokens = await asyncio.gather(*(_login() for _ in range(25)))

    assert tokens == ["tok"] * 25
    assert dashboard.logins == 1, f"25 個並發冷啟動打了 {dashboard.logins} 次登入"
    assert sleeps == []


async def test_twelve_concurrent_mcp_sessions_share_one_login(conn, monkeypatch, sleeps):
    """AI-HER-PERF 重現：比 247 限流（10 次）更多的並發新 MCP session，全部要成功、只登入一次。"""
    dashboard = FakeDashboard(limit=10)
    _install(monkeypatch, dashboard)

    async def one_session():
        async with Client(hermes_server.mcp, mode="legacy") as client:
            return await client.call_tool("hermes_gateway", {"action": "status"})

    results = await asyncio.gather(*(one_session() for _ in range(12)))

    errors = [r for r in results if r.is_error]
    assert not errors, f"{len(errors)} 個 session 失敗：{errors[0].content[0].text[:200]}"
    for r in results:
        assert json.loads(r.content[0].text)["running"] is True
    assert dashboard.logins == 1


async def test_cached_cookie_is_reused_without_new_login(conn, monkeypatch, sleeps):
    dashboard = FakeDashboard()
    _install(monkeypatch, dashboard)

    assert await _login() == "tok"
    assert await _login() == "tok"

    assert dashboard.logins == 1


# ---------------------------------------------------------------------------
# 429：依 Retry-After 或指數退避重試，最多 2 次
# ---------------------------------------------------------------------------
async def test_429_with_retry_after_is_retried_after_that_delay(conn, monkeypatch, sleeps):
    dashboard = FakeDashboard(login_script=[(429, {"retry-after": "3"}), (200, {})])
    _install(monkeypatch, dashboard)

    assert await _login() == "tok"

    assert dashboard.logins == 2
    assert sleeps == [3.0]


async def test_429_without_retry_after_uses_exponential_backoff(conn, monkeypatch, sleeps):
    dashboard = FakeDashboard(login_script=[(429, {}), (429, {}), (200, {})])
    _install(monkeypatch, dashboard)

    assert await _login() == "tok"

    assert dashboard.logins == 3
    assert sleeps == [1.0, 2.0]


async def test_retry_after_is_capped(conn, monkeypatch, sleeps):
    dashboard = FakeDashboard(login_script=[(429, {"retry-after": "3600"}), (200, {})])
    _install(monkeypatch, dashboard)

    assert await _login() == "tok"

    assert sleeps == [hermes_server.LOGIN_RETRY_AFTER_CAP]
    assert hermes_server.LOGIN_RETRY_AFTER_CAP <= 10


async def test_retry_after_http_date_is_honoured(conn, monkeypatch, sleeps):
    when = email.utils.formatdate(time.time() + 5, usegmt=True)
    dashboard = FakeDashboard(login_script=[(429, {"retry-after": when}), (200, {})])
    _install(monkeypatch, dashboard)

    assert await _login() == "tok"

    assert len(sleeps) == 1 and 3.0 <= sleeps[0] <= 5.0


async def test_unparseable_retry_after_falls_back_to_backoff(conn, monkeypatch, sleeps):
    dashboard = FakeDashboard(login_script=[(429, {"retry-after": "soon"}), (200, {})])
    _install(monkeypatch, dashboard)

    assert await _login() == "tok"

    assert sleeps == [1.0]


async def test_persistent_429_gives_up_after_two_retries(conn, monkeypatch, sleeps):
    dashboard = FakeDashboard(limit=0)
    _install(monkeypatch, dashboard)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await _login()

    assert exc_info.value.response.status_code == 429
    assert dashboard.logins == 3          # 1 次 + 重試 2 次
    assert sleeps == [1.0, 2.0]
    assert hermes_server._cookie_cache == {}


async def test_concurrent_callers_share_one_retry_sequence(conn, monkeypatch, sleeps):
    """被限流時，等著的 session 不各自再打登入；第一個重試成功後大家拿同一個 cookie。"""
    dashboard = FakeDashboard(login_script=[(429, {"retry-after": "1"}), (200, {})])
    _install(monkeypatch, dashboard)

    tokens = await asyncio.gather(*(_login() for _ in range(10)))

    assert tokens == ["tok"] * 10
    assert dashboard.logins == 2
    assert sleeps == [1.0]


async def test_failed_login_does_not_poison_later_attempts(conn, monkeypatch, sleeps):
    """一次登入失敗（例如 dashboard 暫時 502）後，下一次呼叫要重新登入，而不是卡住或沿用失敗結果。"""
    dashboard = FakeDashboard(login_script=[(502, {})])
    _install(monkeypatch, dashboard)

    with pytest.raises(httpx.HTTPStatusError):
        await _login()
    assert await _login() == "tok"

    assert dashboard.logins == 2
    assert sleeps == []                   # 只有 429 才重試


async def test_401_with_missing_credentials_is_not_retried(conn, monkeypatch, sleeps):
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "")
    dashboard = FakeDashboard(login_script=[(401, {})])
    _install(monkeypatch, dashboard)

    with pytest.raises(hermes_server.ConnectionNotConfiguredError, match="dashboard_password"):
        await hermes_server._dashboard_login(DASHBOARD, "admin", "")

    assert dashboard.logins == 1
    assert sleeps == []
