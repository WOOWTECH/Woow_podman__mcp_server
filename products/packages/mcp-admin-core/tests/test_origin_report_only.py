"""MCP_ADMIN_ORIGIN_MODE（負責人 2026-11-12：上線時先不擋任何 Origin，看過紀錄再逐個服務改成 enforce）。

- report-only（預設；未設定或空字串也是）：照樣判斷，沒通過的請求記一行 ``decision=would_deny``，回應與 #136 之前的
  正式站逐位元相同（狀態碼、全部標頭含順序、內容；preflight 與實際請求都是）。
- enforce：沒通過就 403、記一行 ``decision=deny``（#136 加上 B 的紀錄）。
- 兩種模式下，沒有 Origin、清單內的來源都與之前相同；非 MCP 路徑與模式無關。
- Headscale 一律 enforce；模式不合法時真的不啟動（真的 uvicorn 子行程，與還原標記的測試相同做法）。

對照組「#136 之前」＝同一個 ``create_app()`` 拿掉 ``McpOriginMiddleware``、app 全域 CORS 換回 0e432c3 的 Starlette
``CORSMiddleware(allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])``，下游（認證、
proxy、上游）完全相同。這組設定直接寫在這裡、不從受測模組 import：受測模組的設定寫錯就會被抓到。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx
import pytest
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from mcp_admin_core import origin_security
from mcp_admin_core.origin_security import (HEADSCALE_ENV, MODE_ENV, DenialLogLimiter, McpOriginMiddleware,
                                           OriginPolicyError, check_origin_policy_at_startup, origin_mode)

TOKEN = "synthetic-report-only-token-0123456789"
PATH = f"/private_{TOKEN}/mcp"
LOGGER = "mcp_admin_core.origin_security"
ENV = "MCP_ADMIN_ALLOWED_ORIGINS"
ALLOWED = "https://claude.ai"
#: 0e432c3 ``packages/mcp-admin-core/mcp_admin_core/app.py`` 的 ``create_app()``：Headscale 以外的產品整個 app 都套這個。
PRE_136_CORS = {"allow_origins": ["*"], "allow_credentials": True, "allow_methods": ["*"], "allow_headers": ["*"]}

#: None＝未設定、""＝空字串：兩者都是 report-only。
MODES = ["report-only", "enforce", None, ""]
MODE_IDS = ["report-only", "enforce", "unset", "empty"]
ORIGIN_CASES = {
    "none": [],
    "allowed": [("origin", ALLOWED)],
    "not_allowed": [("origin", "https://evil.example")],
    "null": [("origin", "null")],
    "duplicate": [("origin", "https://evil.example"), ("origin", ALLOWED)],
    "malformed": [("origin", "https://claude.ai/")],
}
#: 案例 → (原因代碼, 紀錄裡的 origin 顯示值)。
WOULD_DENY = {
    "not_allowed": ("not_allowed", "https://evil.example"),
    "null": ("null", "null"),
    "duplicate": ("duplicate", "https://evil.example,https://claude.ai"),
    "malformed": ("malformed", "https://claude.ai(has=path)"),
}
METHODS = ["POST", "GET", "DELETE", "OPTIONS"]
PREFLIGHT = [("access-control-request-method", "POST"),
             ("access-control-request-headers", "content-type,mcp-session-id,mcp-protocol-version")]
MCP_HEADERS = "authorization, content-type, last-event-id, mcp-protocol-version, mcp-session-id"


def would_deny_line(reason: str, shown: str, env: str = ENV, requested: str | None = None) -> str:
    extra = f' requested="{requested}"' if requested is not None else ""
    return (f'MCP Origin 未通過（僅記錄，照常放行） decision=would_deny mode=report-only reason={reason} '
            f'origin="{shown}"{extra} env={env}')


def deny_line(reason: str, shown: str, env: str = ENV, requested: str | None = None,
              code: str = "ORIGIN_DENIED") -> str:
    extra = f' requested="{requested}"' if requested is not None else ""
    return f'MCP Origin 被拒（403 {code}） decision=deny mode=enforce reason={reason} origin="{shown}"{extra} env={env}'


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    """模組層級的限流器跨測試共用：每個測試換一個新的，免得前一個測試的同一個來源被抑制。"""
    limiter = DenialLogLimiter()
    monkeypatch.setattr(origin_security, "_DENIAL_LIMITER", limiter)
    return limiter


def _set_mode(monkeypatch, mode: str | None) -> None:
    if mode is None:
        monkeypatch.delenv(MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(MODE_ENV, mode)


def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def _assert_no_token_in_any_log(caplog) -> None:
    for record in caplog.records:
        assert TOKEN not in record.getMessage(), record.name
    for line in _lines(caplog):
        assert "private_" not in line and "/mcp" not in line


def _cors(response: httpx.Response) -> dict[str, str]:
    return {k.lower(): v for k, v in response.headers.items() if k.lower().startswith("access-control-")}


def pre_136_reference(app):
    """同一個 app 改回 #136 之前的中介層：拿掉 McpOriginMiddleware、app 全域 CORS 換回 0e432c3 的設定（Starlette 原版類別）。

    其餘中介層、路由、proxy 與上游都相同（RestoreInProgressGuard、InitializeFenceMiddleware 是這次發布的其他變更，
    只管 lifespan 與 initialize，與 CORS 無關；SecurityHeadersMiddleware 本來就略過 /private_*）。
    """
    stack = []
    for entry in app.user_middleware:
        if entry.cls is McpOriginMiddleware:
            continue
        if isinstance(entry.cls, type) and issubclass(entry.cls, CORSMiddleware):
            entry = Middleware(CORSMiddleware, **PRE_136_CORS)
        stack.append(entry)
    assert len(stack) == len(app.user_middleware) - 1
    assert sum(entry.cls is CORSMiddleware for entry in stack) == 1
    app.user_middleware = stack
    app.middleware_stack = None
    return app


def _request(method: str) -> tuple[str, list[tuple[str, str]], bytes]:
    if method == "POST":
        body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}).encode()
        return "POST", [("content-type", "application/json"), ("accept", "application/json, text/event-stream")], body
    if method == "GET":
        return "GET", [("accept", "text/event-stream"), ("mcp-session-id", "synthetic-session")], b""
    if method == "DELETE":
        return "DELETE", [("mcp-session-id", "synthetic-session")], b""
    return "OPTIONS", list(PREFLIGHT), b""


@pytest.fixture
async def gateway(tmp_path, monkeypatch):
    """受測 app（create_app，目前的中介層）、#136 之前的對照 app，與共用的假上游（記錄每次真的轉發）。"""
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.proxy as proxy
    from mcp_admin_core import create_app

    config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    config.update({"mcp_auth_token": TOKEN, "mcp_server": {"port": 18733}})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
    monkeypatch.setattr(store_mod, "_instance", None)
    monkeypatch.setenv(ENV, ALLOWED)
    monkeypatch.delenv(MODE_ENV, raising=False)
    hits: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        hits.append(request.method)
        assert "origin" not in request.headers  # proxy 一律不把 Origin 轉給子行程
        child_headers = {"mcp-session-id": "synthetic-session", "vary": "Accept-Encoding",
                         "access-control-allow-origin": "https://child.invalid"}
        if request.method == "POST":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}, headers=child_headers)
        if request.method == "GET":
            return httpx.Response(200, content=b"event: message\ndata: {}\n\n",
                                  headers={**child_headers, "content-type": "text/event-stream"})
        if request.method == "DELETE":
            return httpx.Response(200, json={}, headers=child_headers)
        return httpx.Response(204, headers={"allow": "GET, POST, DELETE"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as backend:
        monkeypatch.setattr(proxy, "_client", backend)
        # 子行程身分（blocker 3）：閘道只轉發給通過身分檢查的世代。這裡的「子行程」是假上游，
        # 以 managed_proxy_fixture 明確宣告一個已驗證的合成世代（正式程式沒有這個旁路）。
        from managed_proxy_fixture import attach
        attach(monkeypatch, port=18733)
        store_mod.get_config_store()
        subject = create_app(title="report-only subject")
        reference = pre_136_reference(create_app(title="pre-#136 reference"))
        yield subject, reference, hits


async def _served(app, hits, method, headers, body=b"") -> tuple[httpx.Response, int]:
    before = len(hits)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://svc.example") as client:
        response = await client.request(method, PATH + "?session=query-secret-value", headers=headers, content=body)
    return response, len(hits) - before


def _same(got: httpx.Response, want: httpx.Response) -> None:
    assert got.status_code == want.status_code
    assert got.headers.raw == want.headers.raw  # 全部標頭，含順序與重複
    assert got.content == want.content


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("case", list(ORIGIN_CASES))
@pytest.mark.parametrize("mode", MODES, ids=MODE_IDS)
async def test_mode_origin_method_matrix(gateway, monkeypatch, caplog, mode, case, method):
    subject, reference, hits = gateway
    caplog.set_level(logging.DEBUG)
    _set_mode(monkeypatch, mode)
    enforce = mode == "enforce"
    verb, extra, body = _request(method)
    headers = ORIGIN_CASES[case] + extra
    got, reached = await _served(subject, hits, verb, headers, body)
    lines = _lines(caplog)

    if case in WOULD_DENY and enforce:
        assert got.status_code == 403 and got.json() == {"detail": "ORIGIN_DENIED"}
        assert not _cors(got) and "mcp-session-id" not in got.headers
        assert reached == 0
        assert lines == [deny_line(*WOULD_DENY[case])]
    elif case in WOULD_DENY:
        # report-only：與 #136 之前的正式站完全相同，而且真的照常轉發（preflight 由舊設定的 CORS 自己回答）。
        want, reference_reached = await _served(reference, hits, verb, headers, body)
        _same(got, want)
        assert reached == reference_reached == (0 if method == "OPTIONS" else 1)
        assert got.status_code in (200, 204)
        assert got.headers["access-control-allow-origin"] == ORIGIN_CASES[case][0][1]  # 舊設定：照第一個值回
        assert lines == [would_deny_line(*WOULD_DENY[case])]
    elif case == "none":
        want, reference_reached = await _served(reference, hits, verb, headers, body)
        _same(got, want)
        assert reached == reference_reached == 1
        # 沒有 Origin：不經任何 CORS 處理，子行程自己的標頭原樣透傳（假上游只在 POST／GET／DELETE 帶這個標頭）。
        assert got.headers.get("access-control-allow-origin") == (None if method == "OPTIONS" else "https://child.invalid")
        assert lines == []
    else:  # allowed：精確的 CORS 標頭與 preflight 回答，與 enforce 完全相同
        if method == "OPTIONS":
            assert got.status_code == 200 and reached == 0
            assert _cors(got) == {"access-control-allow-origin": ALLOWED, "access-control-allow-credentials": "true",
                                  "access-control-allow-methods": "GET, POST, DELETE",
                                  "access-control-allow-headers": MCP_HEADERS}
            assert got.headers["vary"] == "Origin"
        else:
            assert got.status_code == 200 and reached == 1
            assert got.headers["access-control-allow-origin"] == ALLOWED
            assert got.headers["access-control-allow-credentials"] == "true"
            assert got.headers["access-control-expose-headers"] == "Mcp-Session-Id"
            assert "origin" in got.headers["vary"].lower()
        monkeypatch.setenv(MODE_ENV, "enforce")
        enforced, _ = await _served(subject, hits, verb, headers, body)
        _same(got, enforced)
        assert _lines(caplog) == []
    _assert_no_token_in_any_log(caplog)


# ---- report-only 的交棒：與 #136 之前的 CORSMiddleware 逐則 ASGI 訊息相同 -------------------------------------------

DENY_ORIGINS = [
    [("origin", "https://evil.example")],
    [("origin", "HTTPS://Evil.EXAMPLE:443")],
    [("origin", "http://[::1]:8443")],
    [("origin", "null")],
    [("origin", "")],
    [("origin", "not-an-origin")],
    [("origin", "https://claude.ai/")],
    [("origin", "https://user@claude.ai")],
    [("origin", "https://evil.example"), ("origin", ALLOWED)],
    [("origin", ALLOWED), ("origin", ALLOWED)],
]
REQUESTS = [
    ("POST", [("content-type", "application/json")]),
    ("GET", [("accept", "text/event-stream")]),
    ("DELETE", [("mcp-session-id", "synthetic-session")]),
    ("PUT", []),
    ("PATCH", []),
    ("HEAD", []),
    ("OPTIONS", []),  # 不是 preflight：沒有 Access-Control-Request-Method
    ("OPTIONS", list(PREFLIGHT)),
    ("OPTIONS", [("access-control-request-method", "PUT"), ("access-control-request-headers", "x-anything, X-Other")]),
    ("OPTIONS", [("access-control-request-method", "TRACE")]),  # 舊設定也回 400：不在 Starlette 的 ALL_METHODS
    ("OPTIONS", [("access-control-request-method", "GET"), ("access-control-request-private-network", "true")]),
]
COOKIES = [[], [("cookie", "sid=cookie-secret-value")]]


def _downstream(calls: list[str]):
    async def app(scope, receive, send):
        calls.append(scope["method"])
        status = {"POST": 200, "GET": 200, "DELETE": 202, "HEAD": 200, "OPTIONS": 204}.get(scope["method"], 405)
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"text/event-stream"), (b"vary", b"Accept-Encoding"),
            (b"mcp-session-id", b"synthetic-session"), (b"access-control-allow-origin", b"https://child.invalid"),
            (b"set-cookie", b"child=1; Path=/")]})
        await send({"type": "http.response.body", "body": b"event: one\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"event: two\n\n", "more_body": False})
    return app


async def _asgi(app, method: str, headers: list[tuple[str, str]], path: str = PATH) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": method, "scheme": "https", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": b"token=query-token-value", "client": ("203.0.113.9", 50000),
             "server": ("svc.example", 443), "state": {},
             "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]}
    await app(scope, receive, send)
    return sent


@pytest.mark.parametrize("origins", DENY_ORIGINS, ids=lambda o: "+".join(v for _, v in o) or "empty")
@pytest.mark.parametrize("mode", ["report-only", None, ""], ids=["report-only", "unset", "empty"])
async def test_report_only_hands_would_deny_requests_to_the_pre_136_cors_unchanged(monkeypatch, caplog, origins, mode):
    monkeypatch.setenv(ENV, ALLOWED)
    _set_mode(monkeypatch, mode)
    mismatches = []
    for method, extra in REQUESTS:
        for cookie in COOKIES:
            headers = origins + extra + cookie
            got_calls, want_calls = [], []
            got = await _asgi(McpOriginMiddleware(_downstream(got_calls)), method, headers)
            want = await _asgi(CORSMiddleware(_downstream(want_calls), **PRE_136_CORS), method, headers)
            if (got, got_calls) != (want, want_calls):
                mismatches.append((method, extra, cookie, got, want))
    assert not mismatches
    lines = _lines(caplog)
    assert lines and all(" decision=would_deny mode=report-only " in line for line in lines)
    assert not any(secret in line for line in lines
                   for secret in (TOKEN, "private_", "query-token-value", "cookie-secret-value"))


@pytest.mark.parametrize("requested, reason, shown", [
    ([("access-control-request-method", "PATCH")], "preflight_method", "PATCH"),
    ([("access-control-request-method", "POST"), ("access-control-request-method", "GET")], "preflight_method",
     "POST,GET"),
    ([("access-control-request-method", "POST"), ("access-control-request-headers", "x-unapproved, Content-Type")],
     "preflight_headers", "x-unapproved"),
    ([("access-control-request-method", "GET"), ("access-control-request-headers", "b-extra,a-extra,mcp-session-id")],
     "preflight_headers", "a-extra,b-extra"),
])
async def test_allowed_origin_preflight_outside_the_mcp_scope(monkeypatch, caplog, requested, reason, shown):
    """清單內的來源、preflight 卻要了 MCP 用不到的方法或標頭：enforce 回 403 CORS_PREFLIGHT_DENIED；report-only 也不擋。

    #136 之前的正式站會回 200（舊設定允許所有方法、照抄要求的標頭），所以 report-only 照樣交給舊設定的 CORS，記一行
    would_deny（requested＝要的方法，或清單外的標頭名稱）。
    """
    monkeypatch.setenv(ENV, ALLOWED)
    headers = [("origin", ALLOWED)] + requested
    monkeypatch.setenv(MODE_ENV, "enforce")
    calls: list[str] = []
    denied = await _asgi(McpOriginMiddleware(_downstream(calls)), "OPTIONS", headers)
    assert denied[0]["status"] == 403 and b"CORS_PREFLIGHT_DENIED" in denied[1]["body"]
    assert not [k for k, _ in denied[0]["headers"] if k.lower().startswith(b"access-control-")] and not calls
    monkeypatch.setenv(MODE_ENV, "report-only")
    got_calls, want_calls = [], []
    got = await _asgi(McpOriginMiddleware(_downstream(got_calls)), "OPTIONS", headers)
    want = await _asgi(CORSMiddleware(_downstream(want_calls), **PRE_136_CORS), "OPTIONS", headers)
    assert (got, got_calls) == (want, want_calls) and got[0]["status"] == 200
    assert _lines(caplog) == [deny_line(reason, ALLOWED, requested=shown, code="CORS_PREFLIGHT_DENIED"),
                              would_deny_line(reason, ALLOWED, requested=shown)]


async def test_allowed_and_absent_origins_are_identical_in_both_modes_at_the_middleware(monkeypatch, caplog):
    monkeypatch.setenv(ENV, ALLOWED)
    for method, extra in REQUESTS[:8]:
        for origins in ([], [("origin", ALLOWED)], [("origin", "HTTPS://CLAUDE.AI:443")]):
            results = []
            for mode in ("report-only", "enforce"):
                monkeypatch.setenv(MODE_ENV, mode)
                calls: list[str] = []
                results.append((await _asgi(McpOriginMiddleware(_downstream(calls)), method, origins + extra), calls))
            assert results[0] == results[1], (method, extra, origins)
    assert _lines(caplog) == []


# ---- 紀錄：would_deny 與 deny、不含路徑與 token、限流照舊 ------------------------------------------------------------


def test_report_only_logs_one_would_deny_line_to_stdout_and_ring_buffer_without_secrets(
        make_app, monkeypatch, caplog, capsys):
    from mcp_admin_core.routers import logs

    logs._install_admin_log_capture(("mcp_admin_core",))
    monkeypatch.setenv(ENV, ALLOWED)
    monkeypatch.delenv(MODE_ENV, raising=False)  # 預設＝report-only
    client = make_app({"mcp_auth_token": TOKEN})
    caplog.set_level(logging.DEBUG)
    with logs._buffer_lock:
        ring_before = len([e for e in logs._log_buffer if e["source"] == LOGGER])
    response = client.post(PATH + "?session=query-secret-value&token=query-token-value", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "body-secret-value", "version": "1"}}},
        headers={"Origin": "https://untrusted-client.example", "Cookie": "sid=cookie-secret-value",
                 "Authorization": "Bearer header-secret-value", "X-Forwarded-Host": "forwarded-secret.example",
                 "Accept": "application/json, text/event-stream"})
    # 照常放行：子行程沒在跑，閘道自己回答 initialize（與沒有 Origin 時相同），並帶 #136 之前的 CORS 標頭。
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://untrusted-client.example"
    assert response.headers["access-control-allow-credentials"] == "true"
    expected = would_deny_line("not_allowed", "https://untrusted-client.example")
    assert _lines(caplog) == [expected]
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]
    stdout = [line for line in capsys.readouterr().out.splitlines() if "decision=" in line]
    assert stdout == [f"WARNING:     [{LOGGER}] {expected}"]
    with logs._buffer_lock:
        ring = [e["message"] for e in logs._log_buffer if e["source"] == LOGGER][ring_before:]
    assert ring == [expected]
    for text in (expected, *stdout):
        for secret in (TOKEN, "private_", "/mcp", "query-secret-value", "query-token-value", "cookie-secret-value",
                       "header-secret-value", "forwarded-secret", "body-secret-value"):
            assert secret not in text
    _assert_no_token_in_any_log(caplog)


async def test_would_deny_lines_share_the_rate_limit(monkeypatch, caplog):
    monkeypatch.setenv(ENV, ALLOWED)
    monkeypatch.setenv(MODE_ENV, "report-only")
    calls: list[str] = []
    middleware = McpOriginMiddleware(_downstream(calls))
    for _ in range(50):
        sent = await _asgi(middleware, "POST", [("origin", "https://repeat.example")])
        assert sent[0]["status"] == 200
    assert len(calls) == 50  # 每一次都照常放行
    assert _lines(caplog) == [would_deny_line("not_allowed", "https://repeat.example")]  # 同一個來源 60 秒一行


async def test_logging_failure_never_blocks_a_report_only_request(monkeypatch, caplog):
    monkeypatch.setenv(ENV, ALLOWED)
    monkeypatch.setenv(MODE_ENV, "report-only")

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic logging failure with https://evil.example")

    monkeypatch.setattr(origin_security, "classify_denial", broken)
    calls: list[str] = []
    sent = await _asgi(McpOriginMiddleware(_downstream(calls)), "POST", [("origin", "https://evil.example")])
    assert sent[0]["status"] == 200 and calls == ["POST"]
    assert _lines(caplog) == []


# ---- 非 MCP 路徑與模式無關 -------------------------------------------------------------------------------------------


def test_non_mcp_routes_ignore_the_mode(make_app, monkeypatch):
    monkeypatch.setenv(ENV, ALLOWED)
    client = make_app({"mcp_auth_token": TOKEN})
    seen = []
    for mode in MODES:
        _set_mode(monkeypatch, mode)
        preflight = client.options("/api/auth/login", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        allowed_preflight = client.options("/api/auth/login", headers={
            "Origin": ALLOWED, "Access-Control-Request-Method": "POST"})
        health = client.get("/healthz", headers={"Origin": "https://evil.example", "Cookie": "sid=x"})
        # #136：非 MCP 路徑預設不給任何來源（Starlette 的 400 preflight 仍帶 Allow-Methods 等標頭，但沒有 Allow-Origin）。
        assert preflight.status_code == 400 and allowed_preflight.status_code == 400
        for response in (preflight, allowed_preflight, health):
            assert "access-control-allow-origin" not in response.headers
        assert health.status_code == 200
        seen.append([(r.status_code, r.headers.raw, r.content) for r in (preflight, allowed_preflight, health)])
    assert all(entry == seen[0] for entry in seen)


# ---- 模式的驗證與啟動那一行 --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("env, mode, expected", [
    (ENV, None, ("report-only", "default")),
    (ENV, "", ("report-only", "default")),
    (ENV, "report-only", ("report-only", "MCP_ADMIN_ORIGIN_MODE")),
    (ENV, "enforce", ("enforce", "MCP_ADMIN_ORIGIN_MODE")),
    (HEADSCALE_ENV, None, ("enforce", "headscale")),
    (HEADSCALE_ENV, "report-only", ("enforce", "headscale")),
    (HEADSCALE_ENV, "synthetic-invalid-mode-value", ("enforce", "headscale")),  # Headscale 完全不看這個變數
])
def test_startup_line_names_mode_allowlist_size_and_env(monkeypatch, caplog, env, mode, expected):
    monkeypatch.setenv(env, "https://claude.ai, HTTPS://CLAUDE.AI:443,https://app.example:8443")
    _set_mode(monkeypatch, mode)
    caplog.set_level(logging.INFO, logger=LOGGER)
    policy = check_origin_policy_at_startup(env)
    assert (policy.mode, policy.mode_source, policy.allowlist_entries, policy.env) == (*expected, 2, env)
    lines = _lines(caplog)
    assert len(lines) == 1 and [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.INFO]
    assert lines[0].startswith(f"MCP Origin 政策 mode={expected[0]} mode_source={expected[1]} allowlist_entries=2 "
                               f"env={env}（")
    assert ("would_deny" in lines[0]) == (expected[0] == "report-only")
    assert "synthetic-invalid-mode-value" not in lines[0]


@pytest.mark.parametrize("value", ["Enforce", "ENFORCE", " enforce", "enforce ", "enforce\n", "Report-Only",
                                   "report_only", "reportonly", "block", "off", "true", "  ",
                                   "synthetic-invalid-mode-value"])
def test_invalid_mode_fails_startup_validation_without_echoing_the_value(monkeypatch, caplog, value):
    monkeypatch.setenv(ENV, ALLOWED)
    monkeypatch.setenv(MODE_ENV, value)
    caplog.set_level(logging.INFO, logger=LOGGER)
    with pytest.raises(OriginPolicyError, match="Invalid MCP_ADMIN_ORIGIN_MODE") as raised:
        check_origin_policy_at_startup(ENV)
    assert isinstance(raised.value, RuntimeError)
    lines = _lines(caplog)
    assert len(lines) == 1 and [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.ERROR]
    assert lines[0].startswith("MCP Origin 政策設定錯誤，拒絕啟動 env=MCP_ADMIN_ORIGIN_MODE：")
    assert "report-only" in lines[0] and "enforce" in lines[0]
    if value.strip() and value.strip() not in ("enforce", "report-only", "block", "off", "true"):
        assert value.strip() not in lines[0] and value.strip() not in str(raised.value)
    assert "synthetic-invalid-mode-value" not in lines[0]
    # 與允許值只差大小寫或空白時說出原因（與 chart 相同）
    assert ("大小寫或前後空白" in lines[0]) == (value.strip().lower() in ("enforce", "report-only"))


def test_invalid_mode_stops_the_lifespan_before_any_config_write(make_app, tmp_config_path, monkeypatch):
    monkeypatch.setenv(MODE_ENV, "synthetic-invalid-mode-value")
    client = make_app({"mcp_auth_token": TOKEN})
    before = tmp_config_path.read_bytes()
    with pytest.raises(RuntimeError, match="Invalid MCP_ADMIN_ORIGIN_MODE"):
        with client:
            pass
    assert tmp_config_path.read_bytes() == before  # owner_defaults／自動發 token 都還沒跑


@pytest.mark.parametrize("value", ["Enforce", "synthetic-invalid-mode-value"])
def test_invalid_mode_at_runtime_fails_closed_like_an_invalid_allowlist(make_app, monkeypatch, value):
    """執行中讀到不合法的模式（正式環境的 env 不會變，只有測試會）：與不合法的清單相同，MCP 路徑一律 503。"""
    monkeypatch.setenv(ENV, ALLOWED)
    client = make_app({"mcp_auth_token": TOKEN})
    monkeypatch.setenv(MODE_ENV, value)
    for headers in ({}, {"Origin": ALLOWED}, {"Origin": "https://evil.example"}):
        response = client.get(PATH, headers=headers)
        assert response.status_code == 503 and response.json() == {"detail": "ORIGIN_POLICY_UNAVAILABLE"}
    assert client.get("/healthz").status_code == 200
    with pytest.raises(OriginPolicyError):
        origin_mode(ENV)


# ---- Headscale：一律 enforce，不看 MCP_ADMIN_ORIGIN_MODE --------------------------------------------------------------


@pytest.mark.parametrize("mode", ["report-only", None, "", "synthetic-invalid-mode-value"],
                         ids=["report-only", "unset", "empty", "invalid"])
async def test_headscale_shared_layer_always_enforces(monkeypatch, caplog, mode):
    """共用層本身（不靠 Headscale 自己的 admin_boundary）對 Headscale 一律回 403。"""
    monkeypatch.setenv(HEADSCALE_ENV, "https://admin.example")
    _set_mode(monkeypatch, mode)
    assert origin_mode(HEADSCALE_ENV) == "enforce"
    calls: list[str] = []
    middleware = McpOriginMiddleware(_downstream(calls), env=HEADSCALE_ENV)
    for origins in ([("origin", "https://evil.example")], [("origin", "null")], [("origin", "https://admin.example/")],
                    [("origin", "https://evil.example"), ("origin", "https://admin.example")]):
        for method, extra in REQUESTS:
            sent = await _asgi(middleware, method, origins + extra)
            assert sent[0]["status"] == 403 and b"ORIGIN_DENIED" in sent[1]["body"], (origins, method)
            assert not [k for k, _ in sent[0]["headers"] if k.lower().startswith(b"access-control-")]
    sent = await _asgi(middleware, "OPTIONS", [("origin", "https://admin.example"),
                                               ("access-control-request-method", "PATCH")])
    assert sent[0]["status"] == 403 and b"CORS_PREFLIGHT_DENIED" in sent[1]["body"]
    assert not calls
    allowed = await _asgi(middleware, "POST", [("origin", "https://admin.example")])
    assert allowed[0]["status"] == 200 and calls == ["POST"]
    lines = _lines(caplog)
    assert lines and all(" decision=deny mode=enforce " in line and line.endswith("env=HEADSCALE_MCP_ALLOWED_ORIGINS")
                         for line in lines)
    assert not any("would_deny" in line for line in lines)


async def test_headscale_product_returns_403_with_report_only_set(tmp_path, monkeypatch, caplog):
    """真的 Headscale app（admin_boundary＋共用層）：MCP_ADMIN_ORIGIN_MODE=report-only 時照樣 403、不轉發。"""
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.proxy as proxy
    from headscale_mcp_admin.main import app

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_path / "config.json"))
    store = store_mod.ConfigStore(tmp_path / "config.json")
    await store.put("mcp_auth_token", TOKEN)
    await store.put("mcp_server", {"port": 18734})
    monkeypatch.setattr(store_mod, "_instance", store)
    monkeypatch.setenv(HEADSCALE_ENV, "https://admin.example")
    monkeypatch.setenv(MODE_ENV, "report-only")
    hits: list[str] = []

    def upstream(request):
        hits.append(request.method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as backend:
        monkeypatch.setattr(proxy, "_client", backend)
        # 最後的正向對照要真的轉發到假上游：以 managed_proxy_fixture 宣告一個已驗證的合成世代（blocker 3）。
        from managed_proxy_fixture import attach
        attach(monkeypatch, port=18734)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin.example") as client:
            for origins in ([("origin", "https://evil.example")], [("origin", "null")],
                            [("origin", "https://admin.example/")],
                            [("origin", "https://evil.example"), ("origin", "https://admin.example")]):
                for method in METHODS:
                    verb, extra, body = _request(method)
                    response = await client.request(verb, PATH, headers=origins + extra, content=body)
                    assert response.status_code == 403, (origins, method)
                    assert not _cors(response)
            assert not hits
            good = await client.post(PATH, headers={"Origin": "https://admin.example"},
                                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert good.status_code == 200 and hits == ["POST"]
    lines = _lines(caplog)
    assert lines and all(" decision=deny mode=enforce " in line for line in lines)
    _assert_no_token_in_any_log(caplog)


# ---- 真的 uvicorn（與 Dockerfile CMD 相同的啟動方式，預設 --lifespan auto） ------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _uvicorn(target: str, data: Path, port: int, extra_env: dict[str, str],
             app_dir: Path | None = None) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k not in (MODE_ENV, ENV, HEADSCALE_ENV)}
    env.update({"MCP_ADMIN_CONFIG": str(data / "config.json"), "PYTHONDONTWRITEBYTECODE": "1",
                "JWT_SECRET": "synthetic-jwt-secret-for-origin-mode-tests-0123456789", **extra_env})
    command = [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(port)]
    if app_dir is not None:
        command += ["--app-dir", str(app_dir)]
    return subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _watch_until_exit(proc: subprocess.Popen, port: int, timeout: float = 60) -> bool:
    """行程結束前不停嘗試連線；回傳有沒有連上過。"""
    connected = False
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                connected = True
        except OSError:
            pass
        time.sleep(0.02)
    return connected


def _probe_app(tmp_path: Path) -> Path:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "origin_mode_probe_app.py").write_text(
        "from mcp_admin_core import create_app\napp = create_app(title='origin mode probe')\n")
    return app_dir


def _legacy_config(child: dict) -> dict:
    """沒有 owner_defaults 標記、從來沒有過 token 的舊設定：正常啟動會遷移、自動發 token（設定檔一定會變）。"""
    return {"admin_password": "fixture-admin-password-0123456789", "connection": {},
            "tools": {"disabled": ["fixture_tool"], "disabled_operations": {}},
            "mcp_server": child, "proxy": {"timeout": 86400}}


def test_real_uvicorn_exits_3_on_an_invalid_mode_and_serves_with_a_valid_one(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sentinel = tmp_path / "child-started"
    child = {"command": sys.executable, "port": _free_port(),
             "args": ["-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(120)", str(sentinel)]}
    config = data / "config.json"
    config.write_text(json.dumps(_legacy_config(child)))
    config.chmod(0o600)
    before = config.read_bytes()
    app_dir = _probe_app(tmp_path)

    # 不合法的模式（以及對照：#136 起就有的不合法清單）：exit 3、從沒聽 port、設定檔與子行程都沒動。
    for extra, failing_env in (({MODE_ENV: "Enforce"}, MODE_ENV),
                               ({MODE_ENV: "synthetic-invalid-mode-value"}, MODE_ENV),
                               ({ENV: "*", MODE_ENV: "report-only"}, ENV)):
        port = _free_port()
        proc = _uvicorn("origin_mode_probe_app:app", data, port, extra, app_dir)
        connected = _watch_until_exit(proc, port)
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 3, err[-2000:]
        assert not connected  # 從來沒有開始監聽：沒有任何流量
        assert "Application startup failed. Exiting." in err
        assert "lifespan' protocol appears unsupported" not in err
        assert f"MCP Origin 政策設定錯誤，拒絕啟動 env={failing_env}：" in out  # 我們自己的一行（stdout）
        assert "synthetic-invalid-mode-value" not in out + err  # 不回顯設定值
        assert config.read_bytes() == before and sorted(os.listdir(data)) == ["config.json"]
        assert not sentinel.exists()

    # 對照：模式未設定（＝report-only）時同一份設定照常啟動、拉起子行程、記一行生效政策（證明上面的斷言抓得到）。
    port = _free_port()
    proc = _uvicorn("origin_mode_probe_app:app", data, port, {}, app_dir)
    try:
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline and status != 200:
            assert proc.poll() is None, proc.communicate(timeout=5)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                    status = response.status
            except OSError:
                time.sleep(0.1)
        assert status == 200
        deadline = time.monotonic() + 30
        while not sentinel.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sentinel.exists()
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=30)
    assert ("MCP Origin 政策 mode=report-only mode_source=default allowlist_entries=0 "
            "env=MCP_ADMIN_ALLOWED_ORIGINS（") in out


def test_real_uvicorn_product_app_exits_3_on_an_invalid_mode(tmp_path):
    """產品自己包在 core lifespan 外面的步驟（odoo 的 seed_defaults）不會吞掉這個失敗。"""
    data = tmp_path / "data"
    data.mkdir()
    config = data / "config.json"
    config.write_text(json.dumps(_legacy_config({"port": _free_port()})))
    config.chmod(0o600)
    port = _free_port()
    proc = _uvicorn("odoo_mcp_admin.main:app", data, port, {MODE_ENV: "synthetic-invalid-mode-value"})
    connected = _watch_until_exit(proc, port)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 3, err[-2000:]
    assert not connected
    assert "Application startup failed. Exiting." in err
    assert "MCP Origin 政策設定錯誤，拒絕啟動 env=MCP_ADMIN_ORIGIN_MODE：" in out
    assert "synthetic-invalid-mode-value" not in out + err
