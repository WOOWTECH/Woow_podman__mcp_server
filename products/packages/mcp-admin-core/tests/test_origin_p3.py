"""Origin 的 P3 小修（F4）：Vary 不重複、normalize_origin 只認 ASCII、畸形 Origin 的紀錄不含非 URL 形狀的原文。"""
from __future__ import annotations

import logging
import re

import pytest

from mcp_admin_core import origin_security
from mcp_admin_core.origin_security import DenialLogLimiter, classify_denial, normalize_origin

TOKEN = "synthetic-p3-token-0123456789"
PATH = f"/private_{TOKEN}/mcp"
LOGGER = "mcp_admin_core.origin_security"
ENV = "MCP_ADMIN_ALLOWED_ORIGINS"


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(origin_security, "_DENIAL_LIMITER", DenialLogLimiter())


def _raw_vary_tokens(response) -> list[str]:
    """逐行讀原始標頭（不用會把重複值合併的 API），回傳所有 Vary 值拆開後的 token（小寫）。"""
    tokens: list[str] = []
    for name, value in response.headers.raw:
        if name.lower() == b"vary":
            tokens += [t.strip().lower() for t in value.decode("latin-1").split(",") if t.strip()]
    return tokens


@pytest.mark.parametrize("mode", ["report-only", "enforce"])
@pytest.mark.parametrize("method", ["POST", "GET", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("origin", ["https://claude.ai", "https://evil.example", None])
def test_vary_lists_origin_at_most_once_on_every_mcp_response(make_app, monkeypatch, mode, method, origin):
    monkeypatch.setenv(ENV, "https://claude.ai")
    monkeypatch.setenv("MCP_ADMIN_ORIGIN_MODE", mode)
    client = make_app({"mcp_auth_token": TOKEN})
    headers = {"Accept-Encoding": "gzip"}
    if origin:
        headers["Origin"] = origin
    if method == "OPTIONS":
        headers.update({"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    response = client.request(method, PATH, headers=headers, content=b"{}" if method == "POST" else None)
    assert _raw_vary_tokens(response).count("origin") <= 1, response.headers.raw
    if origin == "https://claude.ai":
        # 清單內來源：一定有 Vary: Origin（快取要依 Origin 區分），且只有一次。
        assert _raw_vary_tokens(response).count("origin") == 1, response.headers.raw


def test_allowed_origin_actual_request_has_exactly_one_origin_in_vary(make_app, monkeypatch):
    monkeypatch.setenv(ENV, "https://claude.ai")
    client = make_app({"mcp_auth_token": TOKEN})
    response = client.post(PATH, headers={"Origin": "https://claude.ai", "Accept-Encoding": "gzip"}, content=b"{}")
    assert response.headers["access-control-allow-origin"] == "https://claude.ai"
    assert _raw_vary_tokens(response).count("origin") == 1, response.headers.raw


def test_vary_helper_keeps_existing_tokens_and_handles_star_and_separate_lines():
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"vary", b"Accept-Encoding"), (b"vary", b"origin")]})
        await send({"type": "http.response.body", "body": b""})

    import asyncio

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": PATH, "raw_path": PATH.encode(), "root_path": "",
             "query_string": b"", "headers": [(b"origin", b"https://claude.ai")]}
    import os
    os.environ[ENV] = "https://claude.ai"
    try:
        asyncio.run(origin_security.McpOriginMiddleware(inner)(scope, receive, send))
    finally:
        del os.environ[ENV]
    varies = [v.decode().lower() for k, v in sent[0]["headers"] if k == b"vary"]
    tokens = [t.strip() for v in varies for t in v.split(",")]
    assert tokens.count("origin") == 1 and "accept-encoding" in tokens


# origin-2
@pytest.mark.parametrize("value", [
    "https://K.example",          # Kelvin sign 在 re.I 下會被當成 k
    "https://claude.aiK",
    "HTTPS://CLAUDE.AI",               # 對照：ASCII 大小寫仍可
    "https://ınfo.example",       # dotless i
    "https://ſhop.example",       # long s
    "https://claude.ai:٣٤٥",  # 非 ASCII 數字
])
def test_normalize_origin_rejects_non_ascii_instead_of_case_folding(value):
    if value == "HTTPS://CLAUDE.AI":
        assert normalize_origin(value) == ("https", "claude.ai", 443)
        return
    with pytest.raises(ValueError):
        normalize_origin(value)
    with pytest.raises(ValueError):
        normalize_origin(value, headscale_legacy=True)


def test_kelvin_sign_does_not_match_an_ascii_allowlist(make_app, monkeypatch):
    monkeypatch.setenv(ENV, "https://k.example")
    monkeypatch.setenv("MCP_ADMIN_ORIGIN_MODE", "enforce")
    client = make_app({"mcp_auth_token": TOKEN})
    response = client.post(PATH, headers=[(b"origin", "https://K.example".encode("utf-8"))],
                           content=b"{}")
    assert response.status_code == 403


# origin-3
@pytest.mark.parametrize("raw", [
    "private_" + TOKEN,
    "Bearer " + TOKEN,
    "bearer   " + TOKEN,
    "sk-" + TOKEN,
    TOKEN,
    "x" * 10_000,
    "https://evil.example\r\nWARNING forged " + TOKEN,
    "Bearer " + TOKEN + "\nnext line",
    "あ" * 50 + TOKEN,
])
def test_malformed_non_url_values_never_appear_verbatim_in_the_log(raw):
    reason, shown = classify_denial([raw])
    assert reason == "malformed"
    assert TOKEN not in shown and "Bearer" not in shown and "private_" not in shown
    assert re.fullmatch(r"[ -~]*", shown) and not re.search(r"[\r\n\"\\\s]", shown)
    assert len(shown) <= 120


def test_malformed_summary_shows_length_and_character_classes():
    _, shown = classify_denial(["Bearer " + TOKEN])
    assert "len=" in shown and str(len("Bearer " + TOKEN)) in shown


def test_malformed_summary_keeps_a_recognisable_scheme_prefix_only():
    _, shown = classify_denial(["ftp://evil.example"])
    assert shown.startswith("ftp://") or "scheme=ftp" in shown
    _, shown = classify_denial(["javascript:alert(1)"])
    assert "alert" not in shown


def test_url_shaped_malformed_values_log_only_scheme_host_port_and_flags():
    _, shown = classify_denial([f"https://svc.example/private_{TOKEN}/mcp?token=query-token-value"])
    assert shown == "https://svc.example(has=path+query)"


def test_duplicate_header_values_also_summarise_non_url_shapes():
    reason, shown = classify_denial(["Bearer " + TOKEN, "https://evil.example"])
    assert reason == "duplicate" and TOKEN not in shown and "https://evil.example" in shown


@pytest.mark.parametrize("raw", [b"private_" + TOKEN.encode(), b"Bearer " + TOKEN.encode(),
                                  b"x" * 10_000, b"https://a.example\r\nWARNING forged " + TOKEN.encode()])
def test_end_to_end_log_line_has_no_raw_malformed_text(caplog, monkeypatch, raw):
    import asyncio
    monkeypatch.setenv(ENV, "https://claude.ai")
    monkeypatch.setenv("MCP_ADMIN_ORIGIN_MODE", "enforce")
    sent = []

    async def inner(scope, receive, send):  # pragma: no cover
        raise AssertionError("denied request reached the application")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": PATH, "raw_path": PATH.encode(), "root_path": "",
             "query_string": b"", "headers": [(b"origin", raw)]}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        asyncio.run(origin_security.McpOriginMiddleware(inner)(scope, receive, send))
    lines = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert sent[0]["status"] == 403 and len(lines) == 1
    assert TOKEN not in lines[0] and "Bearer" not in lines[0] and "forged" not in lines[0]
    assert not re.search(r"[\x00-\x1f\x7f-\x9f]", lines[0])


# ---- F8（r2-sec-3）：URL 形狀的畸形 Origin 只記 scheme://host[:port] 加旗標 ----------------------------------------

S = "SECRET123"


@pytest.mark.parametrize("raw, expected", [
    (f"https://us:{S}@evil.example", "https://evil.example(has=userinfo)"),
    (f"https://{S}@evil.example:8443", "https://evil.example:8443(has=userinfo)"),
    (f"https://evil.example/x?api_key={S}", "https://evil.example(has=path+query)"),
    (f"https://evil.example?api_key={S}", "https://evil.example(has=query)"),
    (f"https://evil.example?password={S}&session={S}", "https://evil.example(has=query)"),
    (f"https://evil.example?x=1;sig={S}", "https://evil.example(has=query)"),
    (f"https://evil.example#access_token={S}", "https://evil.example(has=fragment)"),
    (f"https://evil.example/p?k={S}#frag{S}", "https://evil.example(has=path+query+fragment)"),
    (f"https://u:{S}@evil.example:444/p?k={S}#f", "https://evil.example:444(has=userinfo+path+query+fragment)"),
    (f"HTTP://Us:{S}@EVIL.Example:80/", "http://evil.example(has=userinfo+path)"),
    (f"https://[::1]:8443/x?k={S}", "https://[::1]:8443(has=path+query)"),
    (f"https://{S}@[2001:DB8::1]/", "https://[2001:db8::1](has=userinfo+path)"),
    (f"https://evil.example\\@x.example/?k={S}", "https://evil.example(has=path+query)"),  # 反斜線＝路徑開頭，與瀏覽器一致
    (f"https://evil.example/{S}/mcp", "https://evil.example(has=path)"),
    (f"https://evil.example/private_{S}", "https://evil.example(has=path)"),
    ("https://evil.example/", "https://evil.example(has=path)"),
])
def test_url_shaped_malformed_origin_logs_only_normalised_origin_and_flags(raw, expected):
    reason, shown = classify_denial([raw])
    assert reason == "malformed" and shown == expected
    assert S not in shown and "api_key" not in shown and f"frag{S}" not in shown and "access_token" not in shown


@pytest.mark.parametrize("raw", [
    f"https://us:{S}@",                      # 沒有 host
    f"https://us:{S}@/path",
    f"https://@evil.example:99999/x?k={S}",  # port 超範圍
    f"https://bad_host.example/x?k={S}",     # host 不合規則
    f"https://evil.example:/x?k={S}",
    f"https://[zz::1]/x?k={S}",
    f"https://exaémple.example/x?k={S}",
])
def test_url_shaped_origin_without_a_parsable_host_falls_back_to_the_non_url_summary(raw):
    reason, shown = classify_denial([raw])
    assert reason == "malformed" and shown.startswith("non-url(len=") and S not in shown


def test_url_shaped_origin_with_overlong_host_is_truncated_and_leaks_nothing():
    reason, shown = classify_denial([f"https://{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 60}/x?k={S}"])
    assert reason == "malformed" and len(shown) <= 120 and shown.endswith("...") and S not in shown
    reason, shown = classify_denial([f"https://{'a' * 5000}.example/x?k={S}"])
    assert reason == "malformed" and shown.startswith("non-url(len=") and S not in shown
    reason, shown = classify_denial([f"https://evil.example/{'x' * 100_000}?k={S}"])
    assert shown == "https://evil.example(has=path+query)"


@pytest.mark.parametrize("raw", [
    f"https://us:{S}@evil.example\r\nWARNING forged",
    f"https://evil.example/x?k={S}\x00",
    f"https://evil.example/x?k={S}\x1b[31m",
    f"https://evil.example/x?k={S} tail",
    f"https://evil.example/x?k={S} ",
])
def test_url_shaped_origin_with_control_characters_gets_the_non_url_summary(raw):
    reason, shown = classify_denial([raw])
    assert reason == "malformed" and shown.startswith("non-url(") and S not in shown and "forged" not in shown
    assert re.fullmatch(r"[ -~]*", shown) and not re.search(r"[\r\n\"\\\s]", shown)


def test_duplicate_header_renders_each_url_shaped_value_without_secrets():
    reason, shown = classify_denial([f"https://us:{S}@evil.example/x?k={S}", "https://claude.ai"])
    assert reason == "duplicate" and shown == "https://evil.example(has=userinfo+path+query),https://claude.ai"


def test_headscale_legacy_host_rule_is_kept_for_the_summary():
    # 舊版 Headscale 的 host 驗證允許尾端點；其他產品不允許（走 non-url 摘要）
    assert classify_denial([f"https://evil.example./x?k={S}"], headscale_legacy=True)[1] == "https://evil.example.(has=path+query)"
    assert classify_denial([f"https://evil.example./x?k={S}"])[1].startswith("non-url(")


@pytest.mark.parametrize("raw", [f"https://us:{S}@evil.example", f"https://evil.example/x?api_key={S}",
                                  f"https://evil.example#{S}", f"https://evil.example:8443/x"])
def test_decision_is_unchanged_malformed_url_shapes_are_still_denied(raw):
    """只改 log 內容：這些值仍然不是合法 Origin（拒絕或 would_deny 的判斷不變）。"""
    from mcp_admin_core.origin_security import normalize_origin
    with pytest.raises(ValueError):
        normalize_origin(raw)
    assert classify_denial([raw])[0] == "malformed"
