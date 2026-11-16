"""閘道請求內容位元組預算第三輪：第二輪審查的 3 個 P1 繞過與小項（審查 F1～F9、沙盒發現 3）。

- P1-A（F1）：結構守門把 body 當 UTF-8 逐位元組掃描，但 ``json.loads(bytes)`` 會依 RFC 4627 自動偵測 UTF-16／32；UTF-16 的 ``"``
  是 ``00 22``，掃描器的引號奇偶被打亂、遇到沒結束的字串就放行，15.99 MiB 的 UTF-16 body 讓閘道多出 217 MiB。現在守門前先拒收
  不是 UTF-8 的內容（前 4 位元組含 NUL，或整段不是合法 UTF-8）；合法的 UTF-8（中文、日文、emoji）一律放行。
- P1-B（F2）：閉括號沒有成本上限，``"`` ＋ 30 萬逗號（讓快速路徑失效）＋ 1,500 萬個 ``]`` 讓掃描卡 5.6 秒。現在慢路徑遇到
  ``depth < 0``（閉括號多於開括號，不是合法 JSON）就停。
- P1-C（F3）：一個大字串裡只要有一個非 BMP 字元（😀），Python str 整串變 4 位元組／字元，實測約 15 倍；``AMPLIFICATION`` 提到 16。
- P2-D：結構守門與編碼拒收也記節流 WARNING。 P2-E：補跳脫引號與元素個數邊界測試。 P3-F：涓流上傳整體期限；n8n／Headscale 拒收格式一致。
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import time

import httpx
import pytest

from loopback_servers import RecordHandler, wait_until
from mcp_admin_core import request_budget as rb
from test_gateway_request_budget import _budget_env, gateway  # noqa: F401  (gateway 是 fixture)
from mcp_admin_core.request_limits import (
    DEFAULT_MAX_DEPTH, DEFAULT_MAX_TOTAL_ELEMENTS, McpRequestLimits, RequestLimitError, structure_violation,
)

MIB = 1024 * 1024
N = 16 * MIB


def _violation(body: bytes):
    return structure_violation(body, max_depth=DEFAULT_MAX_DEPTH, max_elements=DEFAULT_MAX_TOTAL_ELEMENTS)


def _envelope(arguments_json: bytes, rpc_id: int = 7) -> bytes:
    return (b'{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{"name":"t","arguments":' % rpc_id
            + arguments_json + b"}}")


# ---------------------------------------------------------------------------
# P1-A：非 UTF-8（UTF-16／32）一律拒收；合法 UTF-8 一律放行
# ---------------------------------------------------------------------------

ENCODINGS = ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]


def _texts():
    pathological = '["∀",' + ",".join(["[]"] * 5000) + "]"
    return {
        "simple": '{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        "review-F1": pathological,
        "cjk": '{"name":"你好，世界"}',
    }


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("name", ["simple", "review-F1", "cjk"])
def test_utf16_and_utf32_bodies_are_refused(encoding, name):
    body = _texts()[name].encode(encoding)
    reason = _violation(body)
    assert reason is not None and "UTF-8" in reason
    # 這就是被擋的原因：json.loads 看得懂它（所以守門前一定要先擋）
    assert json.loads(body) is not None


@pytest.mark.parametrize("bom", [b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00"])
def test_bom_prefixed_bodies_are_refused(bom):
    assert "UTF-8" in _violation(bom + b"[\x00]\x00")
    assert "UTF-8" in _violation(bom)


def test_nul_in_the_first_four_bytes_is_refused_like_json_detect_encoding():
    assert "UTF-8" in _violation(b"[\x00\x00\x00")
    assert "UTF-8" in _violation(b"\x00[")


def test_bytes_that_are_not_valid_utf8_are_refused():
    assert "UTF-8" in _violation('{"a":"é"}'.encode("latin-1"))
    assert "UTF-8" in _violation(b'{"a":"\xff"}')
    assert "UTF-8" in _violation(b'{"a":"\xed\xa0\x80"}')  # UTF-8 編碼的代理字元不合法
    assert "UTF-8" in _violation('{"a":"你好"}'.encode()[:-3])  # 多位元組字元被截斷


LEGIT_UTF8 = [
    '{"name":"你好😀"}',
    '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"t","arguments":{"text":"こんにちは、世界！🎉🚀"}}}',
    '{"mixed":"abc 你好 こんにちは 😀 𝒳 é ñ ∀ \\u4f60 \\ud83d\\ude00"}',
    '["∀","中文",[1,2,{"k":"😀"}]]',
    "﻿" + '{"bom":"utf-8 with BOM 你好"}',
    '{"emoji_only":"😀😀😀😀"}',
]


@pytest.mark.parametrize("text", LEGIT_UTF8)
def test_valid_utf8_with_cjk_and_emoji_passes(text):
    body = text.encode("utf-8")
    assert _violation(body) is None
    json.loads(body)  # 對照：本來就是合法 JSON


def test_empty_and_plain_ascii_bodies_are_unchanged():
    assert _violation(b"") is None
    assert _violation(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}') is None
    assert _violation(b"not json at all") is None  # 不是 JSON 的 ASCII 維持原行為（照舊轉給子行程）


def test_large_valid_utf8_bodies_pass_quickly_and_without_copying_the_whole_body():
    """16 MiB 的 CJK／emoji 字串（合法的大呼叫）放行，時間是線性的、很短。"""
    for char in ("你", "😀", "é"):
        body = _envelope(json.dumps({"text": char * (N // 4)}, ensure_ascii=False).encode("utf-8"))
        started = time.process_time()
        assert _violation(body) is None
        assert time.process_time() - started < 0.5


def test_a_multibyte_character_split_by_the_chunk_boundary_is_not_misjudged():
    from mcp_admin_core import request_limits

    chunk = request_limits._UTF8_CHUNK
    for offset in range(0, 5):  # 讓 3、4 位元組的字元剛好跨過每個檢查區塊的邊界
        body = b'{"a":"' + b"x" * (chunk - 6 - offset) + "你😀你".encode() * 3 + b'"}'
        assert _violation(body) is None


def test_mcp_request_limits_refusal_for_utf16_has_no_content_and_the_usual_format():
    limits = McpRequestLimits(max_bytes=N, max_argument_elements=10)
    body = '{"jsonrpc":"2.0","id":5,"method":"tools/call","secret-marker":1}'.encode("utf-16-le")
    with pytest.raises(RequestLimitError) as caught:
        limits.parse_and_check(body)
    assert "secret-marker" not in json.dumps(caught.value.payload)
    assert caught.value.payload["error"]["code"] == -32602


def test_utf16_refusal_message_is_about_encoding_not_structure():
    """編碼拒收訊息不說「結構太複雜」，而說不是 UTF-8；結構超限的訊息維持原樣（沙盒發現 3）。"""
    limits = McpRequestLimits(max_bytes=N, max_argument_elements=10)
    with pytest.raises(RequestLimitError) as encoding:
        limits.parse_and_check('{"jsonrpc":"2.0","id":5,"method":"tools/call"}'.encode("utf-16-le"))
    message = encoding.value.payload["error"]["message"]
    assert "structure is too complex" not in message and "not UTF-8" in message
    assert encoding.value.kind == "encoding"
    with pytest.raises(RequestLimitError) as structure:
        limits.parse_and_check(_envelope(b"[" * (DEFAULT_MAX_DEPTH + 5) + b"]" * (DEFAULT_MAX_DEPTH + 5)))
    assert "structure is too complex" in structure.value.payload["error"]["message"]
    assert structure.value.kind == "structure"


# ---------------------------------------------------------------------------
# P1-B：閉括號的掃描成本有常數上界
# ---------------------------------------------------------------------------


def _closers_body(total: int, closer: bytes) -> bytes:
    head = b'"' + b"," * 300_000 + b'"'  # 字串裡的逗號讓快速路徑失效，進入慢路徑
    return head + closer * ((total - len(head)) // len(closer))


@pytest.mark.parametrize("closer", [b"]", b"}]", b"}", b"]}"])
def test_closing_bracket_flood_does_not_stall_the_scan(closer):
    body = _closers_body(N, closer)
    started = time.process_time()
    assert _violation(body) is None  # 不合法 JSON：不判違規（json.loads 本來就會拒），但要很快
    elapsed = time.process_time() - started
    assert elapsed < 0.3, f"{elapsed:.2f}s"  # 修正前 5.4～5.6 秒


def test_scan_time_does_not_grow_with_body_size():
    small, large = _closers_body(N // 2, b"]"), _closers_body(N, b"]")
    t_small = time.process_time()
    _violation(small)
    t_small = time.process_time() - t_small
    t_large = time.process_time()
    _violation(large)
    t_large = time.process_time() - t_large
    assert t_large < 2 * t_small + 0.1  # 與 body 大小無關（不翻倍）


def test_closers_hidden_after_balanced_structure_are_bounded_too():
    """先有一些合法結構再灌閉括號：depth 一轉負就停。"""
    body = b"[" + b'","' * 1 + b"," * 300_000 + b"]" + b"]" * (N - 300_100)
    started = time.process_time()
    _violation(body)
    assert time.process_time() - started < 0.3


def test_closers_never_exceed_the_work_bound_for_valid_nesting():
    """合法 JSON 的閉括號數不會超過開括號數，所以不誤擋：深度 64 與大量平衡的小結構照常放行。"""
    assert _violation(b"[" * 64 + b"]" * 64) is None
    assert _violation(b"[" + b",".join([b"[]"] * 90_000) + b"]") is None
    assert "elements" in _violation(b"[" + b",".join([b"[]"] * 120_000) + b"]")  # 仍被元素上限擋下


# ---------------------------------------------------------------------------
# P1-C：放大倍數涵蓋非 BMP 字元的最壞情況
# ---------------------------------------------------------------------------


def test_amplification_covers_the_measured_astral_worst_case():
    """審查 F3 實測（真 uvicorn 閘道＋MCP SDK 子行程）：單一大字串＋一個 😀 約 15.2 倍（原字元）、14.5 倍（跳脫寫法）；
    第三輪審查：astral 字串加守門上限內最壞結構疊加最壞 17.8 倍。鎖住審查發現，將來不得退回 18 以下。"""
    assert rb.AMPLIFICATION >= 18


def test_python_str_really_is_four_bytes_per_character_with_one_astral_character():
    """放大的原因：PEP 393，一個非 BMP 字元讓整個字串每字元 4 位元組；CJK（BMP）是 2；純 ASCII 是 1。"""
    n = 2 * MIB
    ascii_str = json.loads(b'"' + b"a" * n + b'"')
    cjk_str = json.loads(('"' + "你" * (n // 3) + '"').encode())
    astral_str = json.loads(b'"' + b"a" * n + "😀".encode() + b'"')
    escaped = json.loads(b'"' + b"a" * n + b'\\ud83d\\ude00"')
    assert sys.getsizeof(ascii_str) < 1.1 * n
    assert 1.9 * (n // 3) * 1 < sys.getsizeof(cjk_str) < 2.2 * (n // 3)  # 2 位元組／字元，來源 3 位元組／字元（約 0.67 倍）
    assert sys.getsizeof(astral_str) > 3.9 * n and sys.getsizeof(escaped) > 3.9 * n  # 4 倍，原字元與跳脫寫法一樣


def test_two_gib_pod_cannot_be_oom_killed_by_astral_requests_that_fit_the_budget(monkeypatch):
    """審查 F3 的 2 GiB Odoo：全域預算全用滿、每個請求都是 15.2 倍，也不超過 cgroup 上限減安全邊際。"""
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
    from mcp_admin_core import profiles

    set_launch_policy(LaunchPolicy(command="x", max_extra_profiles=5, profile_child_memory_mib=100))
    monkeypatch.setattr(rb, "_memory_limit_mib", lambda: 2048)
    monkeypatch.setattr(profiles, "_memory_limit_mib", lambda: 2048)
    try:
        limits = rb.BudgetLimits.from_env()
    finally:
        set_launch_policy(None)
    worst = limits.total / MIB * 15.2
    assert rb.GATEWAY_BASELINE_MIB + 6 * 100 + worst <= 2048 - rb.SAFETY_MARGIN_MIB + 1, (limits.total / MIB, worst)


def test_one_gib_production_default_is_unchanged_at_the_single_request_floor(monkeypatch):
    """正式站全是 1 GiB：預算本來就在 16 MiB 下限（一個請求），提高倍數不會讓它更差。"""
    from mcp_admin_core.launch_policy import LaunchPolicy, set_launch_policy
    from mcp_admin_core import profiles

    monkeypatch.setattr(rb, "_memory_limit_mib", lambda: 1024)
    monkeypatch.setattr(profiles, "_memory_limit_mib", lambda: 1024)
    for extra, child in ((5, 100), (3, 160), (2, 300)):  # Odoo、Hermes、n8n
        set_launch_policy(LaunchPolicy(command="x", max_extra_profiles=extra, profile_child_memory_mib=child))
        try:
            limits = rb.BudgetLimits.from_env()
        finally:
            set_launch_policy(None)
        assert limits.total == 16 * MIB and limits.per_profile == 16 * MIB


def test_astral_request_is_charged_and_refused_under_a_small_budget():
    """登記量是 body 位元組數（倍數在全域預算的推導裡）：兩個 15 MiB 的 astral 請求放不進 16 MiB 的預算，被拒。"""
    budget = rb.RequestBudget(rb.BudgetLimits(total=16 * MIB, per_profile=16 * MIB))
    first = budget.open("default")
    first.grow(15 * MIB, cap=16 * MIB)
    second = budget.open("default")
    with pytest.raises(rb.BudgetExceeded):
        second.grow(15 * MIB, cap=16 * MIB)
    first.finish()
    second.finish()
    assert budget.used() == 0


def test_plain_ascii_big_string_is_still_accepted_by_the_structure_guard():
    body = _envelope(b'{"text":"' + b"x" * (N - 4096) + b'"}')
    assert _violation(body) is None


# ---------------------------------------------------------------------------
# P2-E：補審查變異 R5（跳脫引號）、R6（元素邊界）的測試
# ---------------------------------------------------------------------------


def test_escaped_quote_does_not_end_the_string():
    """字串裡的 ``\\"`` 不結束字串：之後字串內的逗號不算元素（拿掉跳脫處理的變異會判違規）。"""
    body = b'["a\\"' + b"," * 50 + b'"]'
    assert structure_violation(body, max_depth=64, max_elements=10) is None


def test_escaped_backslash_before_a_quote_does_end_the_string():
    """``\\\\"``（跳脫的反斜線再接引號）結束字串：之後的逗號是真的元素（把 ``\\\\`` 當成跳脫引號的變異會漏掉）。"""
    body = b'["a\\\\"' + b"," * 50 + b"]"
    assert "elements" in structure_violation(body, max_depth=64, max_elements=10)


def test_string_ending_with_backslash_pairs_and_quotes_in_the_slow_path():
    """反斜線成對／不成對：``\\\\"`` 結束字串（之後的逗號是真的元素），``\\\\\\"`` 是跳脫的引號（字串繼續，逗號不算）。"""
    ends = b'["' + b"x\\\\" + b'"' + b"," * 40 + b"]"  # 內容 x\\ ：兩對反斜線，引號結束
    continues = b'["' + b"x\\\\\\" + b'"' + b"," * 40 + b'"]'  # 三個反斜線＋引號：跳脫，字串繼續到最後的引號
    assert "elements" in structure_violation(ends, max_depth=64, max_elements=30)
    assert structure_violation(continues, max_depth=64, max_elements=30) is None


def test_element_boundary_is_exactly_the_limit_on_the_fast_path():
    limit = DEFAULT_MAX_TOTAL_ELEMENTS
    at = b"[" + b",".join([b"0"] * limit) + b"]"  # 200,000 個元素
    over = b"[" + b",".join([b"0"] * (limit + 1)) + b"]"  # 200,001 個
    assert _violation(at) is None
    assert "elements" in _violation(over)


def test_element_boundary_is_exactly_the_limit_on_the_slow_path():
    """字串裡塞滿逗號讓快速路徑失效，真正的元素數剛好等於上限放行、多一個就拒（``>`` 改 ``>=`` 的變異會紅）。"""
    limit = DEFAULT_MAX_TOTAL_ELEMENTS
    noise = b'"' + b"," * 5000 + b'"'

    def make(elements: int) -> bytes:  # 一個容器＋(elements-1) 個逗號
        return b"[" + noise + b",0" * (elements - 1) + b"]"

    exact = make(limit)  # 容器 1 ＋ 逗號 limit-1，字串雜訊裡的逗號不算
    assert _violation(exact) is None
    assert "elements" in _violation(make(limit + 1))


# ---------------------------------------------------------------------------
# P2-D：結構守門／編碼拒收的節流 WARNING
# ---------------------------------------------------------------------------


@pytest.fixture
def plain_gateway(make_app, monkeypatch, synthetic_child):
    from mcp_admin_core import proxy

    forwarded = []

    def upstream(request):
        forwarded.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {"content": []}})

    monkeypatch.setattr(proxy, "_client", None)
    monkeypatch.setattr(proxy, "_get_client",
                        lambda timeout=86400: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    return make_app({"mcp_auth_token": "secret"}), forwarded


def _post(client, body: bytes, path="mcp"):
    return client.post("/private_secret/" + path, content=body,
                       headers={"content-type": "application/json", "accept": "application/json, text/event-stream"})


def test_utf16_pathological_body_is_refused_and_json_loads_is_never_called(plain_gateway, monkeypatch):
    from mcp_admin_core import tool_filter

    client, forwarded = plain_gateway

    def boom(body):  # json.loads 的入口：被呼叫就代表記憶體已經被吃掉
        raise AssertionError("parse_jsonrpc must not run for a non-UTF-8 body")

    monkeypatch.setattr(tool_filter, "parse_jsonrpc", boom)
    for encoding in ENCODINGS:
        body = ('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"a":["∀",' + ",".join(["[]"] * 100) + "]}}").encode(encoding)
        response = _post(client, body)
        assert response.status_code == 413 and response.json()["id"] is None, encoding  # 內容是 UTF-16，認不出 id：與其他結構拒收一致
        assert response.json()["error"]["code"] == -32602 and "UTF-8" in response.json()["error"]["message"]
    assert not forwarded


def test_valid_utf8_cjk_and_emoji_is_forwarded_and_parsed(plain_gateway):
    client, forwarded = plain_gateway
    body = '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"t","arguments":{"text":"你好😀こんにちは"}}}'.encode()
    response = _post(client, body)
    assert response.status_code == 200 and "error" not in response.json()
    assert len(forwarded) == 1 and forwarded[0].content == body


def test_structure_refusals_log_one_throttled_warning_per_profile(plain_gateway, monkeypatch):
    from mcp_admin_core import gateway_pool

    client, forwarded = plain_gateway
    monkeypatch.setattr(gateway_pool, "_reports", {})
    now = [5000.0]
    monkeypatch.setattr(gateway_pool.time, "monotonic", lambda: now[0])
    bad = _envelope(b"[" * (DEFAULT_MAX_DEPTH + 1) + b'"secret-marker"' + b"]" * (DEFAULT_MAX_DEPTH + 1))
    utf16 = '{"jsonrpc":"2.0","id":7,"secret-marker":1}'.encode("utf-16-le")

    def lines():
        return [m for m in logs.messages() if "拒收請求內容" in m]

    with RecordHandler("mcp_admin_core.gateway_pool") as logs:
        for _ in range(4):
            assert _post(client, bad).status_code == 200
        assert len(lines()) == 1 and "suppressed" not in lines()[0]
        _post(client, utf16)  # 編碼拒收也走同一個節流（另一個情況，自己有一行）
        assert len(lines()) == 2
        now[0] += gateway_pool.REPORT_INTERVAL_SECONDS + 1
        _post(client, bad)
        assert len(lines()) == 3 and "suppressed=3" in lines()[2]
        text = " ".join(logs.messages())
    assert "secret-marker" not in text and "secret" not in text.replace("rejected", "")  # 不含內容或 token
    assert not forwarded


# ---------------------------------------------------------------------------
# P3-F：涓流上傳的整體期限、n8n／Headscale 的拒收格式
# ---------------------------------------------------------------------------


def test_total_upload_deadline_env_is_validated(monkeypatch):
    for bad in ("0", "-3", "abc", "nan", "999999"):
        monkeypatch.setenv(rb.BODY_TOTAL_ENV, bad)
        with RecordHandler("mcp_admin_core") as logs:
            assert rb.BudgetLimits.from_env().body_total_seconds == rb.DEFAULT_BODY_TOTAL_SECONDS
        assert rb.BODY_TOTAL_ENV in " ".join(logs.messages())
    monkeypatch.setenv(rb.BODY_TOTAL_ENV, "42")
    assert rb.BudgetLimits.from_env().body_total_seconds == 42
    assert 120 <= rb.DEFAULT_BODY_TOTAL_SECONDS <= 300  # 15 MiB 要在 2～5 分鐘內讀完


def test_a_trickling_upload_is_dropped_at_the_total_deadline_even_if_it_never_goes_idle(gateway, monkeypatch):
    """F5：每隔不到閒置上限就送 1 位元組，過去可以永遠占著預算；現在整體期限一到就放棄、歸還。"""
    _budget_env(monkeypatch, 16, 16)
    monkeypatch.setenv("MCP_GATEWAY_BODY_IDLE_SECONDS", "2")
    monkeypatch.setenv(rb.BODY_TOTAL_ENV, "3")
    with gateway() as gw:
        head = (f"POST {gw.path()} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                f"Accept: application/json, text/event-stream\r\nContent-Length: {15 * MIB}\r\n\r\n")
        sock = socket.create_connection(("127.0.0.1", int(gw.base.rsplit(":", 1)[1])))
        try:
            sock.sendall(head.encode())
            wait_until(lambda: gw.used() >= 15 * MIB, 5)
            started = time.monotonic()
            reply = b""
            sock.settimeout(0.5)
            while time.monotonic() - started < 8 and not reply:
                try:
                    sock.sendall(b" ")  # 每約 0.5 秒一個位元組：永遠不會閒置 2 秒
                    reply = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
            assert reply.startswith(b"HTTP/1.1 408"), reply[:100]
            assert time.monotonic() - started < 6
            wait_until(lambda: gw.used() == 0, 5)
        finally:
            sock.close()
