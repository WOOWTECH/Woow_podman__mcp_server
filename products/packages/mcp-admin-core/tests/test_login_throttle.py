"""管理登入暴力破解限流（AI-FIX-SEC-1／AI-HER-008-4）。

10 次輸錯鎖該用戶端 IP 300 秒（429＋Retry-After，正確密碼也擋）；以 IP 為鍵、不是帳號；
正確登入清掉計數；平行猛打不會溜過；表大小有上限；環境變數可調、可停用。
"""

from __future__ import annotations

import threading

import pytest

from mcp_admin_core.auth.login_throttle import LoginThrottle, client_key


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ── LoginThrottle 單元 ──────────────────────────────────────────────────────


def test_locks_on_the_max_failures_th_attempt_and_expires():
    clk = Clock()
    t = LoginThrottle(3, 60, 300, clock=clk)
    assert [t.begin("a"), t.begin("a")] == [0, 0]
    assert t.retry_after("a") == 0
    assert t.begin("a") == 0          # attempt #3 is still evaluated ...
    assert t.retry_after("a") == 300  # ... but the lock is on
    assert t.begin("a") == 300        # attempt #4 is refused, not evaluated
    clk.t += 100
    assert t.begin("a") == 200        # remaining time, the lock is not extended
    clk.t += 200
    assert t.retry_after("a") == 0
    assert t.begin("a") == 0 and t.retry_after("a") == 0   # counter restarted from 0


def test_window_expiry_forgets_old_failures():
    clk = Clock()
    t = LoginThrottle(3, 60, 300, clock=clk)
    t.begin("a"); t.begin("a")
    clk.t += 61
    t.begin("a"); t.begin("a")
    assert t.retry_after("a") == 0    # 2 in the new window, not 4
    t.begin("a")
    assert t.retry_after("a") == 300


def test_keys_are_independent_and_success_resets():
    t = LoginThrottle(3, 60, 300, clock=Clock())
    t.begin("a"); t.begin("a")
    t.succeed("a")
    t.begin("a"); t.begin("a")
    assert t.retry_after("a") == 0    # reset by the successful login
    t.begin("b"); t.begin("b"); t.begin("b")
    assert t.retry_after("b") == 300 and t.retry_after("a") == 0


def test_success_clears_a_lock_made_by_the_provisional_count():
    t = LoginThrottle(2, 60, 300, clock=Clock())
    t.begin("a")
    assert t.begin("a") == 0          # the 2nd attempt locks, and is evaluated
    t.succeed("a")                    # ... and it was the right password
    assert t.retry_after("a") == 0


def test_disabled_when_max_failures_is_not_positive():
    for off in (0, -1):
        t = LoginThrottle(off, 60, 300, clock=Clock())
        assert [t.begin("a") for _ in range(50)] == [0] * 50 and not t.enabled


def test_table_is_bounded(monkeypatch):
    import mcp_admin_core.auth.login_throttle as mod

    monkeypatch.setattr(mod, "_MAX_TRACKED", 50)
    clk = Clock()
    t = LoginThrottle(10, 60, 300, clock=clk)
    for i in range(500):
        t.begin("ip-%d" % i)
    assert len(t._state) <= 50


def test_parallel_attempts_cannot_slip_past_the_limit():
    t = LoginThrottle(10, 60, 300, clock=Clock())
    passed = []
    def go():
        passed.append(t.begin("a") == 0)
    ts = [threading.Thread(target=go) for _ in range(100)]
    [x.start() for x in ts]; [x.join() for x in ts]
    assert sum(passed) == 10          # exactly max_failures attempts get evaluated


def test_from_env(monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_LOGIN_MAX_FAILURES", "4")
    monkeypatch.setenv("MCP_ADMIN_LOGIN_WINDOW_SECONDS", "30")
    monkeypatch.setenv("MCP_ADMIN_LOGIN_LOCK_SECONDS", "45")
    t = LoginThrottle.from_env()
    assert (t.max_failures, t.window, t.lock_seconds) == (4, 30.0, 45)
    monkeypatch.setenv("MCP_ADMIN_LOGIN_MAX_FAILURES", "nope")
    assert LoginThrottle.from_env().max_failures == 10


# ── client_key ──────────────────────────────────────────────────────────────


class _Req:
    def __init__(self, headers=None, host="10.0.0.9"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = type("C", (), {"host": host})() if host else None


def test_client_key_prefers_a_valid_cf_connecting_ip():
    assert client_key(_Req({"CF-Connecting-IP": "203.0.113.7"})) == "203.0.113.7"
    assert client_key(_Req({"CF-Connecting-IP": "2001:DB8::1"})) == "2001:db8::1"
    assert client_key(_Req({"CF-Connecting-IP": "not-an-ip"})) == "10.0.0.9"
    assert client_key(_Req({"CF-Connecting-IP": "1.2.3.4, 5.6.7.8"})) == "10.0.0.9"
    assert client_key(_Req({"X-Forwarded-For": "6.6.6.6", "X-Real-IP": "6.6.6.6"})) == "10.0.0.9"
    assert client_key(_Req(host=None)) == "unknown"


# ── 登入 API ────────────────────────────────────────────────────────────────


def _login(c, pw, ip=None, **hdrs):
    h = dict(hdrs)
    if ip:
        h["CF-Connecting-IP"] = ip
    return c.post("/api/auth/login", json={"password": pw}, headers=h)


def test_login_locks_after_ten_wrong_passwords(make_app):
    c = make_app()  # admin_password = "admin"
    codes = [_login(c, "wrong%d" % i, "203.0.113.10").status_code for i in range(15)]
    assert codes == [401] * 10 + [429] * 5
    r = _login(c, "admin", "203.0.113.10")
    assert r.status_code == 429                       # a correct password does not get in while locked
    assert r.headers["retry-after"] == "300"
    assert "Too many" in r.json()["detail"]
    assert "token" not in r.text
    assert _login(c, "admin", "203.0.113.20").status_code == 200   # another client is not affected
    assert _login(c, "admin", "203.0.113.10", **{"X-Forwarded-For": "203.0.113.20"}).status_code == 429


def test_the_lock_is_per_client_ip_not_per_account_so_a_stranger_cannot_lock_the_admin_out(make_app):
    c = make_app()
    for i in range(12):
        _login(c, "x%d" % i, "198.51.100.66")
    assert _login(c, "admin", "198.51.100.1").status_code == 200


def test_a_correct_login_resets_the_counter(make_app):
    c = make_app()
    for i in range(9):
        assert _login(c, "bad%d" % i, "203.0.113.30").status_code == 401
    assert _login(c, "admin", "203.0.113.30").status_code == 200
    for i in range(9):
        assert _login(c, "bad%d" % i, "203.0.113.30").status_code == 401   # not locked early
    assert _login(c, "admin", "203.0.113.30").status_code == 200


def test_invalid_bodies_are_not_counted(make_app):
    c = make_app()
    for _ in range(30):
        assert c.post("/api/auth/login", json={"nope": 1}, headers={"CF-Connecting-IP": "203.0.113.40"}).status_code == 422
    assert _login(c, "admin", "203.0.113.40").status_code == 200


def test_lock_expires(make_app):
    c = make_app()
    clk = Clock()
    c.app.state.login_throttle = LoginThrottle(10, 120, 300, clock=clk)
    for i in range(11):
        _login(c, "bad%d" % i, "203.0.113.50")
    assert _login(c, "admin", "203.0.113.50").status_code == 429
    clk.t += 301
    assert _login(c, "admin", "203.0.113.50").status_code == 200


def test_env_disables_the_limit(make_app, monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_LOGIN_MAX_FAILURES", "0")
    c = make_app()
    assert {_login(c, "bad%d" % i, "203.0.113.60").status_code for i in range(40)} == {401}


def test_env_thresholds_apply(make_app, monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_LOGIN_MAX_FAILURES", "3")
    monkeypatch.setenv("MCP_ADMIN_LOGIN_LOCK_SECONDS", "45")
    c = make_app()
    codes = [_login(c, "bad%d" % i, "203.0.113.70").status_code for i in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    assert _login(c, "admin", "203.0.113.70").headers["retry-after"] in ("45", "44")


def test_429_carries_the_security_headers(make_app):
    c = make_app()
    for i in range(10):
        _login(c, "bad%d" % i, "203.0.113.80")
    r = _login(c, "admin", "203.0.113.80")
    assert r.status_code == 429 and r.headers["x-frame-options"] == "DENY"
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_each_app_has_its_own_throttle(make_app):
    a = make_app(); b = make_app()
    for i in range(11):
        _login(a, "bad%d" % i, "203.0.113.90")
    assert _login(a, "admin", "203.0.113.90").status_code == 429
    assert _login(b, "admin", "203.0.113.90").status_code == 200
