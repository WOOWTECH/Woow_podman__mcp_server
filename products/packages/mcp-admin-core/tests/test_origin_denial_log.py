"""MCP Origin 被拒時記一行 WARNING：只有原因代碼、正規化來源與 env 名稱；限流、狀態有界、回應不變。

正式站的 allowlist 只會有 ``https://claude.ai``；維運要能很快看出真實用戶端是不是送了別的 Origin，但這一行不能
變成洩漏 URL token 或灌爆 log 的管道。

這個檔驗的是 enforce（403 與 ``decision=deny``）：預設自 2026-11-12 起是 report-only，所以明確設定（見 ``enforce_mode``）；
report-only 的 ``decision=would_deny`` 見 ``test_origin_report_only.py``。
"""
from __future__ import annotations

import asyncio
import logging
import re

import pytest

from mcp_admin_core import origin_security
from mcp_admin_core.origin_security import (DENIAL_LOG_MAX_KEYS, DenialLogLimiter, McpOriginMiddleware,
                                           classify_denial, log_denial, sanitize_origin_text)

TOKEN = "synthetic-origin-log-token-0123456789"
PATH = f"/private_{TOKEN}/mcp"
LOGGER = "mcp_admin_core.origin_security"
BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"marker": "body-secret-value"}}


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    """模組層級的限流器跨測試共用：每個測試換一個新的，免得前一個測試的同一個來源被抑制。"""
    limiter = DenialLogLimiter()
    monkeypatch.setattr(origin_security, "_DENIAL_LIMITER", limiter)
    return limiter


@pytest.fixture(autouse=True)
def enforce_mode(monkeypatch):
    """這個檔的 403 與「被拒」紀錄都是 enforce 的行為：預設改成 report-only 之後要明確設定。"""
    monkeypatch.setenv("MCP_ADMIN_ORIGIN_MODE", "enforce")


def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def _ring(source: str = LOGGER) -> list[str]:
    from mcp_admin_core.routers import logs

    with logs._buffer_lock:
        return [entry["message"] for entry in logs._log_buffer if entry["source"] == source]


def _expected(reason: str, shown: str, env: str = "MCP_ADMIN_ALLOWED_ORIGINS") -> str:
    return f'MCP Origin 被拒（403 ORIGIN_DENIED） decision=deny mode=enforce reason={reason} origin="{shown}" env={env}'


def test_denied_origin_logs_one_line_to_stdout_and_ring_buffer_without_path_or_secrets(
        make_app, monkeypatch, caplog, capsys):
    from mcp_admin_core.routers import logs

    logs._install_admin_log_capture(("mcp_admin_core",))  # 各 app 的 create_logs_router 會裝
    monkeypatch.setenv("MCP_ADMIN_ALLOWED_ORIGINS", "https://claude.ai")
    client = make_app({"mcp_auth_token": TOKEN})
    caplog.set_level(logging.DEBUG)
    ring_before = len(_ring())
    response = client.post(PATH + "?session=query-secret-value&token=query-token-value", json=BODY, headers={
        "Origin": "https://untrusted-client.example", "Cookie": "sid=cookie-secret-value",
        "Authorization": "Bearer header-secret-value", "X-Forwarded-Host": "forwarded-secret.example"})

    # 回應與加 log 之前相同：403 ORIGIN_DENIED、沒有任何 CORS 標頭。
    assert response.status_code == 403
    assert response.json() == {"detail": "ORIGIN_DENIED"}
    assert not [name for name in response.headers if name.lower().startswith("access-control-")]

    expected = _expected("not_allowed", "https://untrusted-client.example")
    assert _lines(caplog) == [expected]
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]
    stdout = [line for line in capsys.readouterr().out.splitlines() if "ORIGIN_DENIED" in line]
    assert stdout == [f"WARNING:     [{LOGGER}] {expected}"]  # 容器 stdout（kubectl logs）
    assert _ring()[ring_before:] == [expected]  # Logs 頁 ring buffer
    for text in (expected, *stdout):
        for secret in (TOKEN, "private_", "/mcp", "query-secret-value", "query-token-value", "cookie-secret-value",
                       "header-secret-value", "forwarded-secret", "body-secret-value"):
            assert secret not in text


@pytest.mark.parametrize("headers, reason, shown", [
    ([("Origin", "https://claude.ai/")], "malformed", "https://claude.ai(has=path)"),
    ([("Origin", "null")], "null", "null"),
    ([("Origin", "")], "malformed", "non-url(len=0,classes=none)"),  # 非 URL 形狀只記摘要（origin-3）
    ([("Origin", "https://evil.example"), ("Origin", "https://claude.ai")], "duplicate",
     "https://evil.example,https://claude.ai"),
    ([("Origin", "HTTPS://Evil.EXAMPLE:443")], "not_allowed", "https://evil.example"),
    ([("Origin", "http://evil.example:8080")], "not_allowed", "http://evil.example:8080"),
    ([("Origin", "http://[::1]:8443")], "not_allowed", "http://[::1]:8443"),
    ([("Origin", "https://user@claude.ai")], "malformed", "https://claude.ai(has=userinfo)"),
])
def test_reason_codes_and_normalized_origins(make_app, monkeypatch, caplog, headers, reason, shown):
    monkeypatch.setenv("MCP_ADMIN_ALLOWED_ORIGINS", "https://claude.ai")
    client = make_app({"mcp_auth_token": TOKEN})
    response = client.post(PATH, headers=headers, json=BODY)
    assert response.status_code == 403 and response.json() == {"detail": "ORIGIN_DENIED"}
    assert _lines(caplog) == [_expected(reason, shown)]


def test_allowed_absent_and_non_mcp_origins_produce_no_line(make_app, monkeypatch, caplog):
    monkeypatch.setenv("MCP_ADMIN_ALLOWED_ORIGINS", "https://claude.ai")
    client = make_app({"mcp_auth_token": TOKEN})
    allowed = client.post(PATH, headers={"Origin": "https://claude.ai"}, json=BODY)
    native = client.post(PATH, json=BODY)
    assert allowed.status_code != 403 and native.status_code != 403
    client.get("/healthz", headers={"Origin": "https://evil.example"})  # 非 MCP 路徑不經過這道檢查
    assert _lines(caplog) == []


def _asgi_denial(raw_origins: list[bytes]) -> list[dict]:
    """直接以 ASGI 送出原始標頭位元組（HTTP client 會先擋掉 CR/LF 等值，這裡不經過 client）。"""
    sent: list[dict] = []

    async def inner(scope, receive, send):  # pragma: no cover - 被拒的請求不會到這裡
        raise AssertionError("denied request reached the application")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": PATH, "raw_path": PATH.encode(), "root_path": "",
             "query_string": b"token=query-token-value", "headers": [(b"origin", value) for value in raw_origins]}
    asyncio.run(McpOriginMiddleware(inner)(scope, receive, send))
    return sent


@pytest.mark.parametrize("raw", [
    b"https://evil.example\r\nWARNING forged line reason=not_allowed",
    b"\x1b[31mhttps://evil.example\x1b[0m",
    b'https://evil.example" env=FORGED origin="https://claude.ai',
    b"https://evil.example\\x0a",
    b"https://\xff\xfeevil.example",
    b"https://evil.example/private_" + TOKEN.encode() + b"?token=query-token-value",
    b"https://" + b"a" * 10_000,
    b"\x00" * 300,
])
def test_malicious_origin_values_are_escaped_truncated_and_cannot_inject(caplog, monkeypatch, raw):
    monkeypatch.setenv("MCP_ADMIN_ALLOWED_ORIGINS", "https://claude.ai")
    sent = _asgi_denial([raw])
    assert sent[0]["status"] == 403
    lines = _lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert not re.search(r"[\x00-\x1f\x7f-\x9f]", line)  # 沒有任何控制字元：不能換行、不能下終端機控制碼
    match = re.fullmatch(r'MCP Origin 被拒（403 ORIGIN_DENIED） decision=deny mode=enforce reason=(\w+) origin="([^"]*)" '
                         r'env=MCP_ADMIN_ALLOWED_ORIGINS', line)
    assert match, line  # 顯示值裡沒有未跳脫的雙引號：偽造不了欄位
    shown = match.group(2)
    assert len(shown) <= 120
    assert all("!" <= char <= "~" for char in shown)
    assert TOKEN not in line and "query-token-value" not in line


def test_sanitizer_escapes_everything_outside_printable_ascii():
    assert sanitize_origin_text("https://ok.example") == "https://ok.example"
    assert sanitize_origin_text('a b\t"c"\\\r\n\x7féK\U0001f600') == (
        "a\\x20b\\x09\\x22c\\x22\\x5c\\x0d\\x0a\\x7f\\xe9\\u212a\\U0001f600")
    long = sanitize_origin_text("\x01" * 1000)
    assert len(long) <= 120 and long.endswith("...") and "\\x01" in long
    assert len(sanitize_origin_text("x" * 120)) == 120 and not sanitize_origin_text("x" * 120).endswith("...")
    assert sanitize_origin_text("x" * 121) == "x" * 117 + "..."


def test_duplicate_shows_at_most_four_values():
    reason, shown = classify_denial([f"https://d{i}.example" for i in range(9)])
    assert reason == "duplicate" and shown.count(",") == 4 and shown.endswith(",...") and len(shown) <= 120


def test_headscale_env_keeps_its_legacy_trailing_dot_rendering():
    assert classify_denial(["https://admin.example."], headscale_legacy=True) == (
        "not_allowed", "https://admin.example.")
    assert classify_denial(["https://admin.example."])[0] == "malformed"


def test_one_line_per_origin_per_interval_global_cap_then_one_summary(caplog):
    now = [1000.0]
    limiter = DenialLogLimiter(clock=lambda: now[0])
    for _ in range(50):
        log_denial(["https://repeat.example"], limiter=limiter)
    assert len(_lines(caplog)) == 1  # 同一個來源 60 秒內只記一行
    for i in range(30):
        log_denial([f"https://distinct{i}.example"], limiter=limiter)
    assert len(_lines(caplog)) == 20  # 全部每 60 秒最多 20 行
    now[0] += 59.9
    log_denial(["https://repeat.example"], limiter=limiter)
    assert len(_lines(caplog)) == 20
    now[0] += 0.2  # 新的時間窗：先補一行抑制數，再記這次
    log_denial(["https://repeat.example"], limiter=limiter)
    lines = _lines(caplog)
    assert len(lines) == 22
    assert re.match(r"MCP Origin 未通過紀錄已抑制 suppressed=61（", lines[20])  # 49 + 11 + 1
    assert lines[21] == _expected("not_allowed", "https://repeat.example")
    now[0] += 61
    log_denial(["https://repeat.example"], limiter=limiter)
    assert _lines(caplog)[22:] == [_expected("not_allowed", "https://repeat.example")]  # 沒有抑制就沒有摘要


def test_state_is_bounded_under_a_flood_of_distinct_origins(caplog):
    now = [0.0]
    limiter = DenialLogLimiter(clock=lambda: now[0])
    windows = 0
    for i in range(20_000):
        if i % 1000 == 0:
            now[0] += 61
            windows += 1
        log_denial([f"https://flood-{i}.example"], limiter=limiter)
        assert limiter.size() <= 2 * limiter.max_per_window
    # 每窗最多 20 行加一行摘要
    assert len(_lines(caplog)) <= windows * (limiter.max_per_window + 1)
    tight = DenialLogLimiter(max_per_window=10 ** 9, max_keys=8, clock=lambda: 5.0)
    for i in range(1000):
        log_denial([f"https://hard-cap-{i}.example"], limiter=tight)
        assert tight.size() <= 8
    assert DENIAL_LOG_MAX_KEYS >= 2 * origin_security.DENIAL_LOG_MAX_PER_WINDOW


async def test_summary_is_flushed_when_the_window_ends_without_new_rejections(caplog):
    limiter = DenialLogLimiter(interval=0.05, window=0.05)
    log_denial(["https://burst.example"], limiter=limiter)
    for _ in range(5):
        log_denial(["https://burst.example"], limiter=limiter)
    assert len(_lines(caplog)) == 1
    await asyncio.sleep(0.3)  # 窗結束時由事件迴圈的計時器補一行（每窗最多一個計時器）
    lines = _lines(caplog)
    assert len(lines) == 2 and lines[1].startswith("MCP Origin 未通過紀錄已抑制 suppressed=5（")
    log_denial(["https://burst.example"], limiter=limiter)
    assert not any("suppressed=" in line for line in _lines(caplog)[2:])  # 不會重複補記


def test_logging_failure_never_changes_the_response(monkeypatch, caplog):
    monkeypatch.setenv("MCP_ADMIN_ALLOWED_ORIGINS", "https://claude.ai")

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic logging failure with https://evil.example")

    monkeypatch.setattr(origin_security, "classify_denial", broken)
    sent = _asgi_denial([b"https://evil.example"])
    assert sent[0]["status"] == 403 and b"ORIGIN_DENIED" in sent[1]["body"]
    assert _lines(caplog) == []


def test_duplicate_values_are_escaped_exactly_once():
    reason, shown = classify_denial(["https://a.example\r\n", "HTTPS://B.Example"])
    # 含換行的值不是 URL 形狀：只記摘要（origin-3），正常的值仍只跳脫一次
    assert (reason, shown) == ("duplicate", "non-url(len=19,classes=alpha+control+punct,scheme=https),https://b.example")
    reason, shown = classify_denial(["https://a.example/x\\y", "HTTPS://B.Example"])
    assert (reason, shown) == ("duplicate", "https://a.example(has=path),https://b.example")


def test_url_credentials_in_a_malformed_origin_are_masked_without_the_global_record_factory():
    """設定錯的用戶端把整個 MCP 網址當 Origin：不靠 create_app 安裝的 record factory 也不會記出 token。"""
    reason, shown = classify_denial([f"https://svc.example/private_{TOKEN}/mcp?token=query-token-value"])
    assert reason == "malformed" and TOKEN not in shown and "query-token-value" not in shown
    assert shown == "https://svc.example(has=path+query)"
    _, many = classify_denial(["https://a.example", f"https://svc.example/private_{TOKEN}"])
    assert TOKEN not in many
