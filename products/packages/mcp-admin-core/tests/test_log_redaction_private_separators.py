"""N7（2026-11-13 多帳號實測，mp-verify-n8n 複核）：``/private_`` 後面先接空白、引號、問號等字元時，access log 不可留下 token。

背景：e0a80d6 的遮罩先把整串 URL 解碼再比對 ``/private_<token>``，token 區段遇到「解碼後」的空白、引號、``?``、``#``、``/``
就停（6dc34c8 起），所以 ``/private_%20<token>/mcp``、``/private_%22<token>``、``/private_%3F<token>``、
``/private_%C2%A0<token>``（不換行空白）、``/private_%E3%80%80<token>``（全形空白）、被引號或空白包住的 token 等 23 種寫法
在 uvicorn access log（容器 stdout）留下完整 token，token 中間夾一個這類字元的 4 種寫法留下後半段。8297aa4 只比對字面，
這些都會遮。另有 4 種新舊版都漏：``/private_?<token>``、``/private_//<token>``、``/private_%2F<token>``、
``/private_x/mcp?<token>``。證據：artifacts/mcp-security-release-20261112/mp-verify-n8n-20261113/
（receipts/V-regression-rev1-8297aa4-vs-rev2-e0a80d6.json、offline/uvicorn-real-diff.json）與
mp-n8n-20261113/receipts/N7-token-in-access-log-after-encoded-separator.json。

這裡把複核用的 97 種請求寫法（mp-verify-n8n-20261113/harness/cases.py）原樣搬來：

- 以 uvicorn 0.54（httptools）實際寫進 access log 的 path，經兩個出口——容器 stdout（uvicorn 預設 LOGGING_CONFIG 的
  AccessFormatter）與 Logs 頁 ring buffer（``routers/logs.py`` 的 ``_AdminLogHandler``）——token 不可留下任何
  ``MIN_FRAGMENT`` 個字元以上的片段；兩個出口的請求列相同（同一份遮罩），而且 path 仍可讀（``/private_***/…``，
  不是整段換成 ``[redacted: …]``；例外只有 e0a80d6 就判定「無法判斷」的寫法，見 ``AMBIGUOUS_CASES``，刻意不放寬）。
- 真的 uvicorn（httptools）收 97 個原始請求列，兩個出口同樣檢查（與 harness/offline_diff.py 同樣的做法）。

另有：正常路徑逐字不變、遮罩冪等、病態長路徑（6 萬個 ``%20``、``&=`` 交錯…）單次遮罩的時間上限與「沒有每段一次的
Python 工作」、與 8297aa4 字面規則的差分 fuzz（8297aa4 遮掉的，新版一定也遮掉）。所有 token 都是合成值。
"""
from __future__ import annotations

import base64
import hashlib
import io
import itertools
import logging
import random
import re
import socket
import sys
import threading
import time
from urllib.parse import quote, unquote

import pytest

from mcp_admin_core import log_redaction as lr
from mcp_admin_core.routers import logs as logs_router

# 合成 token（不是任何服務的憑證）：管理頁產生的 64 個 hex，與手動設定允許的 URL-safe 字元（``[A-Za-z0-9_-]``）。
HEX_TOKEN = hashlib.sha256(b"woow-mcp synthetic path token for N7 log redaction tests").hexdigest()
URLSAFE_TOKEN = "S_" + base64.urlsafe_b64encode(
    hashlib.sha256(b"woow-mcp synthetic urlsafe token for N7 log redaction tests").digest()).decode().rstrip("=") + "-q"
TOKENS = {"hex": HEX_TOKEN, "urlsafe": URLSAFE_TOKEN}

# 「不能留下半個 token」：比半個嚴格得多，任何 8 個連續字元的片段都不可出現。
MIN_FRAGMENT = 8


def _fragment(text: str, token: str) -> str | None:
    """text 裡出現的 token 片段（長度 ≥ MIN_FRAGMENT 的任何一段都算；沒有就 None）。"""
    for start in range(len(token) - MIN_FRAGMENT + 1):
        piece = token[start:start + MIN_FRAGMENT]
        if piece in text:
            return piece
    return None


# ---------------------------------------------------------------------------
# 97 種請求寫法：mp-verify-n8n-20261113/harness/cases.py 原樣搬來（每個 target 都是線上送出的 ASCII 請求目標；
# 非 ASCII 分隔字元一律以百分比編碼送出，``lit_first_*`` 則送出字面的 ASCII 字元，就像 curl "…/private_'$TOKEN'/mcp"）。
# 清單本身（與 harness 相同）刻意只用 ASCII：非 ASCII 字元一律寫成跳脫。
# ---------------------------------------------------------------------------


def _enc(s: str) -> str:
    return quote(s, safe="")


ENCODED_FIRST = [
    ('space', ' '), ('tab', '\t'), ('lf', '\n'), ('cr', '\r'), ('vt', '\x0b'), ('ff', '\x0c'), ('fs_1c', '\x1c'),
    ('us_1f', '\x1f'), ('nel_85', '\x85'), ('nbsp_a0', '\xa0'), ('ogham_1680', '\u1680'), ('enquad_2000', '\u2000'),
    ('thinsp_2009', '\u2009'), ('zwsp_200b', '\u200b'), ('linesep_2028', '\u2028'), ('nnbsp_202f', '\u202f'),
    ('mmsp_205f', '\u205f'), ('ideographic_space_3000', '\u3000'), ('bom_feff', '\ufeff'),
    ('dquote', '"'), ('squote', "'"), ('question', '?'), ('hash', '#'), ('slash', '/'), ('backslash', '\\'),
    ('percent', '%'), ('amp', '&'), ('semicolon', ';'), ('equals', '='), ('plus', '+'), ('comma', ','), ('colon', ':'),
    ('at', '@'), ('star', '*'), ('lt', '<'), ('gt', '>'), ('backtick', '`'), ('lbrace', '{'), ('rbrace', '}'),
    ('pipe', '|'), ('caret', '^'), ('lbracket', '['), ('rbracket', ']'), ('bang', '!'), ('dollar', '$'), ('lparen', '('),
    ('rparen', ')'), ('tilde', '~'), ('dot', '.'), ('dash', '-'), ('underscore', '_'),
    ('fullwidth_quote_ff02', '\uff02'), ('left_dquote_201c', '\u201c'), ('right_squote_2019', '\u2019'),
]

LITERAL_FIRST = [
    ("squote", "'"), ("dquote", '"'), ("lt", "<"), ("gt", ">"), ("lbrace", "{"), ("rbrace", "}"), ("pipe", "|"),
    ("caret", "^"), ("backtick", "`"), ("lbracket", "["), ("rbracket", "]"), ("bang", "!"), ("dollar", "$"),
    ("lparen", "("), ("rparen", ")"), ("star", "*"), ("comma", ","), ("semicolon", ";"), ("equals", "="), ("at", "@"),
    ("colon", ":"), ("plus", "+"), ("backslash", "\\"), ("space", " "),
]


def build_targets(token: str) -> dict[str, str]:
    """case 名稱 → 線上送出的請求目標（順序與 harness 相同）。"""
    half = len(token) // 2
    out = {"canonical": f"/private_{token}/mcp", "canonical_no_suffix": f"/private_{token}"}
    for name, char in ENCODED_FIRST:
        out["enc_first_" + name] = f"/private_{_enc(char)}{token}/mcp"
    for name, char in LITERAL_FIRST:
        out["lit_first_" + name] = f"/private_{char}{token}/mcp"
    out.update({
        "wrap_dquote_encoded": f"/private_%22{token}%22/mcp",
        "wrap_squote_literal": f"/private_'{token}'/mcp",
        "wrap_squote_encoded": f"/private_%27{token}%27/mcp",
        "wrap_space_encoded": f"/private_%20{token}%20/mcp",
        "wrap_angle_encoded": f"/private_%3C{token}%3E/mcp",
        "wrap_smart_dquotes": f"/private_{_enc(chr(0x201c))}{token}{_enc(chr(0x201d))}/mcp",
        "mid_space": f"/private_{token[:half]}%20{token[half:]}/mcp",
        "mid_dquote": f"/private_{token[:half]}%22{token[half:]}/mcp",
        "mid_ideographic_space": f"/private_{token[:half]}%E3%80%80{token[half:]}/mcp",
        "mid_question_encoded": f"/private_{token[:half]}%3F{token[half:]}/mcp",
        "end_space": f"/private_{token}%20/mcp",
        "end_dquote": f"/private_{token}%22/mcp",
        "query_literal_q_before_token": f"/private_?{token}/mcp",
        "query_token_param": f"/private_x/mcp?token={token}",
        "query_bare_token": f"/private_x/mcp?{token}",
        "double_slash_after_prefix": f"/private_//{token}/mcp",
        "double_leading_slash": f"//private_{token}/mcp",
    })
    return out


CASES = list(build_targets(HEX_TOKEN))

# 複核報告的分類（V-regression-rev1-8297aa4-vs-rev2-e0a80d6.json）：8297aa4 會遮、e0a80d6 漏的 23 種（其中 mid_* 4 種漏半個），
# 與兩版都漏的 4 種。這裡只用來確認搬來的矩陣與報告一致；所有 97 種都要遮。
REGRESSED_IN_E0A80D6 = {
    "enc_first_dquote", "enc_first_enquad_2000", "enc_first_ideographic_space_3000", "enc_first_linesep_2028",
    "enc_first_mmsp_205f", "enc_first_nbsp_a0", "enc_first_nel_85", "enc_first_nnbsp_202f", "enc_first_ogham_1680",
    "enc_first_question", "enc_first_space", "enc_first_squote", "enc_first_thinsp_2009", "lit_first_dquote",
    "lit_first_squote", "mid_dquote", "mid_ideographic_space", "mid_question_encoded", "mid_space",
    "wrap_dquote_encoded", "wrap_space_encoded", "wrap_squote_encoded", "wrap_squote_literal",
}
LEAKED_IN_BOTH = {"double_slash_after_prefix", "enc_first_slash", "query_bare_token", "query_literal_q_before_token"}

# 解碼後出現控制字元或新的 ``#`` 的 8 種寫法：e0a80d6 就把整段 path 換成「無法判斷」（複核的 MASKED_AMBIGUOUS，沒有外洩），
# 這次刻意維持（「無法判斷」的判定一律不放寬）。
AMBIGUOUS_CASES = {"enc_first_tab", "enc_first_lf", "enc_first_cr", "enc_first_vt", "enc_first_ff", "enc_first_fs_1c",
                   "enc_first_us_1f", "enc_first_hash"}

# 遮罩後 access log 顯示的 path；沒列出的一律是 /private_***/mcp（只有 token 換成 ***，路徑其他部分照舊）。
EXPECTED_SHOWN = {
    "canonical_no_suffix": "/private_***",
    "query_token_param": "/private_***/mcp?token=***",
    "query_bare_token": "/private_***/mcp?***",
    "double_leading_slash": "//private_***/mcp",
    **{case: lr.AMBIGUOUS for case in AMBIGUOUS_CASES},
}


def expected_shown(case: str, token: str) -> str:
    if case == "enc_first_percent" and not re.fullmatch(r"[0-9A-Fa-f]{2}", token[:2]):
        # ``%25`` 解碼後是 ``%`` 接 token 開頭兩個字元；不是十六進位就是網址裡的無效跳脫，e0a80d6 就判定無法判斷（維持）
        return lr.AMBIGUOUS
    return EXPECTED_SHOWN.get(case, "/private_***/mcp")


def test_matrix_is_the_reviewed_97_cases():
    assert len(CASES) == 97
    assert REGRESSED_IN_E0A80D6 <= set(CASES) and LEAKED_IN_BOTH <= set(CASES)
    assert len(REGRESSED_IN_E0A80D6) == 23 and len(LEAKED_IN_BOTH) == 4
    for token in TOKENS.values():
        assert 16 <= len(token) <= 256 and re.fullmatch(r"[A-Za-z0-9_-]+", token)  # 管理頁接受的 token 形狀
        assert all(target.isascii() for target in build_targets(token).values())


def _uvicorn_access_path(target: str) -> str | None:
    """uvicorn 0.54 httptools 實作寫進 access log 的 path：``quote(unquote(path))`` 加上原樣的 query。

    httptools 拒絕的請求目標（例如字面空白）回 None：真的 uvicorn 回 400，不寫 access log（見真 uvicorn 的測試）。
    """
    import httptools
    from uvicorn.protocols.utils import get_path_with_query_string

    try:
        parsed = httptools.parse_url(target.encode("ascii"))
    except httptools.HttpParserInvalidURLError:
        return None
    path = parsed.path.decode("ascii")
    if "%" in path:
        path = unquote(path)
    return get_path_with_query_string({"path": path, "query_string": parsed.query or b""})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 兩個出口：容器 stdout 與 Logs 頁 ring buffer
# ---------------------------------------------------------------------------


class _Sinks:
    """``uvicorn.access`` 只留兩個出口：容器 stdout 與 Logs 頁 ring buffer（見 :func:`access_sinks`）。"""

    def __init__(self, logger: logging.Logger, stream: io.StringIO, writes: list[tuple[str, str]]):
        self.logger = logger
        self._stream = stream
        self._writes = writes
        self.ring_stores_access = False

    def read(self) -> tuple[str, list[str]]:
        """(容器 stdout 的文字, Logs 頁 ring buffer 這段期間收到的 uvicorn.access 行)。"""
        return self._stream.getvalue(), [line for source, line in list(self._writes) if source == "uvicorn.access"]

    def reset(self) -> None:
        self._stream.seek(0)
        self._stream.truncate()
        self._writes.clear()


@pytest.fixture
def access_sinks(monkeypatch):
    """容器 stdout（uvicorn 預設 LOGGING_CONFIG 的 AccessFormatter）與 Logs 頁 ring buffer（``routers/logs.py`` 的
    ``_AdminLogHandler``，與 ``create_logs_router`` 裝的相同）。

    ring buffer 這一側記下 handler 交給 ``_append_log_line_sync`` 的每一行（照常寫進緩衝區），不依賴緩衝區的內部結構。
    e0a80d6 的 Logs 頁會存 uvicorn.access（存在 ``__access__``，頁面上不顯示）；Logs 頁每帳號緩衝區的修正（多帳號實測
    P3-e，另一個 PR）改成完全不存。開始前先送一行探測，``ring_stores_access`` 記下目前是哪一種：有存，每一行都必須是
    遮好、與 stdout 逐字相同的一行；沒存，就一行都不能有。
    """
    from uvicorn.config import LOGGING_CONFIG
    from uvicorn.logging import AccessFormatter

    lr.install()
    stream = io.StringIO()
    stdout = logging.StreamHandler(stream)
    stdout.setFormatter(AccessFormatter(LOGGING_CONFIG["formatters"]["access"]["fmt"], use_colors=False))
    ring = logs_router._AdminLogHandler()
    ring.setFormatter(logging.Formatter("%(message)s"))  # 與 _install_admin_log_capture 相同
    writes: list[tuple[str, str]] = []
    original = logs_router._append_log_line_sync

    def recording(line, *args, **kwargs):
        writes.append((kwargs.get("source", args[0] if args else ""), line))
        original(line, *args, **kwargs)

    monkeypatch.setattr(logs_router, "_append_log_line_sync", recording)
    logger = logging.getLogger("uvicorn.access")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    logger.handlers = [stdout, ring]  # 其他測試（create_app）裝的 handler 先拿掉，免得同一筆寫兩次
    logger.setLevel(logging.INFO)
    logger.propagate = False
    sinks = _Sinks(logger, stream, writes)
    try:
        _log_access(logger, "/healthz", client="127.0.0.1:1", status=200)
        probe_stdout, probe_ring = sinks.read()
        assert probe_stdout.count("/healthz") == 1
        sinks.ring_stores_access = bool(probe_ring)
        sinks.reset()
        yield sinks
    finally:
        logger.handlers, logger.level, logger.propagate = saved[0], saved[1], saved[2]


def _log_access(logger: logging.Logger, path: str, client: str = "127.0.0.1:5555", status: int = 403) -> None:
    """與 uvicorn httptools_impl 相同的呼叫（模板與 5 個參數）。"""
    logger.info('%s - "%s %s HTTP/%s" %d', client, "POST", path, "1.1", status)


_STDOUT_LINE = re.compile(r'INFO:     127\.0\.0\.1:5555 - "POST (.*) HTTP/1\.1" 403 Forbidden')


@pytest.mark.parametrize("token_kind", list(TOKENS))
@pytest.mark.parametrize("case", CASES)
def test_access_log_masks_token_in_stdout_and_ring_buffer(access_sinks, case, token_kind):
    token = TOKENS[token_kind]
    target = build_targets(token)[case]
    path = _uvicorn_access_path(target)
    if path is None:
        # 只有字面空白會被 httptools 拒絕（真的 uvicorn 回 400、沒有 access log）；這裡另外驗「假如有伺服器接受它」
        # 會被 quote 成的樣子，與 enc_first_space 相同。
        assert case == "lit_first_space"
        path = quote(unquote(target))
    _log_access(access_sinks.logger, path)
    stdout, ring = access_sinks.read()
    lines = stdout.splitlines()
    assert len(lines) == 1, lines
    assert len(ring) == (1 if access_sinks.ring_stores_access else 0), ring
    for sink, text in [("stdout", lines[0])] + [("ring buffer", line) for line in ring]:
        assert _fragment(text, token) is None, f"{case} {sink}: {text}"
    shown = _STDOUT_LINE.fullmatch(lines[0])
    assert shown, lines[0]
    # 同一份遮罩：ring buffer 存的是同一筆 record，請求列逐字相同
    assert ring == ([f'127.0.0.1:5555 - "POST {shown.group(1)} HTTP/1.1" 403'] if ring else [])
    # 仍可讀：只有 token 換成 ***，路徑的其他部分照舊（AMBIGUOUS_CASES 以外不會整段換成 [redacted: …]）
    assert shown.group(1) == expected_shown(case, token), shown.group(1)


@pytest.fixture
def app_logger_sinks(monkeypatch):
    """一般 app logger（``mcp_admin_core`` 底下）的兩個出口：容器 stdout 與 Logs 頁 ring buffer（目前帳號的緩衝區）。

    閘道與子行程把網址寫進 log 時走這條路；不論 Logs 頁存不存 access log，這裡的紀錄一定進 ring buffer。
    """
    lr.install()
    stream = io.StringIO()
    stdout = logging.StreamHandler(stream)
    stdout.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    ring = logs_router._AdminLogHandler()
    ring.setFormatter(logging.Formatter("%(message)s"))
    writes: list[str] = []
    original = logs_router._append_log_line_sync

    def recording(line, *args, **kwargs):
        writes.append(line)
        original(line, *args, **kwargs)

    monkeypatch.setattr(logs_router, "_append_log_line_sync", recording)
    logger = logging.getLogger("mcp_admin_core.n7_log_redaction_test")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    logger.handlers = [stdout, ring]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield logger, lambda: (stream.getvalue(), list(writes))
    finally:
        logger.handlers, logger.level, logger.propagate = saved[0], saved[1], saved[2]


@pytest.mark.parametrize("token_kind", list(TOKENS))
@pytest.mark.parametrize("case", CASES)
def test_app_log_lines_mask_token_in_stdout_and_ring_buffer(app_logger_sinks, case, token_kind):
    logger, read = app_logger_sinks
    token = TOKENS[token_kind]
    target = build_targets(token)[case]
    path = _uvicorn_access_path(target) or quote(unquote(target))
    logger.warning("proxy %s %s failed: %s", "POST", path, "upstream timeout")
    stdout, ring = read()
    assert len(ring) == 1, ring
    for sink, text in (("stdout", stdout), ("ring buffer", ring[0])):
        assert _fragment(text, token) is None, f"{case} {sink}: {text}"
    assert stdout == f"WARNING mcp_admin_core.n7_log_redaction_test {ring[0]}\n"  # 同一份遮罩
    shown = expected_shown(case, token)
    # 一般訊息無法判斷時整句換掉（不只網址那一段），與 access log 只換 path 不同
    assert ring[0] == (lr.AMBIGUOUS if shown == lr.AMBIGUOUS else f"proxy POST {shown} failed: upstream timeout"), ring[0]


@pytest.mark.parametrize("token_kind", list(TOKENS))
@pytest.mark.parametrize("case", CASES)
def test_redaction_is_idempotent_for_every_case(case, token_kind):
    token = TOKENS[token_kind]
    target = build_targets(token)[case]
    path = _uvicorn_access_path(target) or quote(unquote(target))
    texts = [path, f'127.0.0.1:5555 - "POST {path} HTTP/1.1" 403']
    if " " not in target:
        # 原始請求目標本身也遮（子行程或例外訊息直接印出網址時）。字面空白例外：一般訊息裡 ``/private_`` 後面的字面空白
        # 就是網址的結尾，而這種請求列 uvicorn 直接回 400，不會進到任何 log。
        texts.append(target)
    for text in texts:
        once = lr.redact(text)
        assert _fragment(once, token) is None, (case, text, once)
        assert lr.redact(once) == once, (case, once, lr.redact(once))
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5555", "POST", path, "1.1", 403), None)
    lr.scrub_record(record)
    first = record.args
    record.args = tuple(first)  # 新的 tuple：指紋對不上，一定會再遮一次
    lr.scrub_record(record)
    assert record.args == first


# ---------------------------------------------------------------------------
# 真的 uvicorn（httptools）：97 個原始請求列，容器 stdout 與 ring buffer 都不可有 token
# ---------------------------------------------------------------------------


async def _forbidden_app(scope, receive, send):
    """與閘道的 token 檢查相同：每個 HTTP 請求都回 403（access log 由 uvicorn 自己寫）。"""
    if scope["type"] != "http":
        return
    more = True
    while more:
        message = await receive()
        more = message.get("more_body", False)
    await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'{"detail":"Invalid or revoked MCP auth token"}'})


def _send_raw(port: int, target: str) -> tuple[int, int | None]:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        local_port = conn.getsockname()[1]
        conn.sendall(b"POST " + target.encode("ascii") + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    status = int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/1.1 ") else None
    return local_port, status


@pytest.mark.parametrize("token_kind", list(TOKENS))
def test_real_uvicorn_access_log_masks_all_97_targets(access_sinks, token_kind):
    import uvicorn

    token = TOKENS[token_kind]
    error_logger = logging.getLogger("uvicorn.error")
    saved_error = (error_logger.propagate, list(error_logger.handlers))
    error_logger.propagate, error_logger.handlers = False, [logging.NullHandler()]  # 400 的 warning 不印到測試輸出
    config = uvicorn.Config(_forbidden_app, http="httptools", loop="asyncio", lifespan="off", log_config=None,
                            access_log=True)
    server = uvicorn.Server(config)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        sent = {case: _send_raw(port, target) for case, target in build_targets(token).items()}
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        error_logger.propagate, error_logger.handlers = saved_error
    assert not thread.is_alive()
    stdout, ring = access_sinks.read()
    ring_text = "\n".join(ring)
    assert _fragment(stdout, token) is None, stdout
    assert _fragment(ring_text, token) is None, ring_text
    line = re.compile(r'127\.0\.0\.1:(\d+) - "POST (.*) HTTP/1\.1"')
    stdout_by_port = {int(m.group(1)): m.group(2) for m in line.finditer(stdout)}
    ring_by_port = {int(m.group(1)): m.group(2) for m in line.finditer(ring_text)}
    for case, (local_port, status) in sent.items():
        if case == "lit_first_space":
            # 字面空白讓請求列不合法：uvicorn 回 400，沒有 access log
            assert status == 400 and local_port not in stdout_by_port and local_port not in ring_by_port
            continue
        assert status == 403, (case, status)
        assert stdout_by_port[local_port] == expected_shown(case, token), (case, stdout_by_port.get(local_port))
        if access_sinks.ring_stores_access:
            assert ring_by_port[local_port] == stdout_by_port[local_port], (case, ring_by_port.get(local_port))
    assert len(stdout_by_port) == 96
    assert len(ring_by_port) == (96 if access_sinks.ring_stores_access else 0)


# ---------------------------------------------------------------------------
# 正常路徑：完整 access 紀錄路徑上逐字不變（或只把 token 換成 ***），不會整行遮掉
# ---------------------------------------------------------------------------

def _numbered(kind: str, params: list) -> dict:
    """參數裡有合成 token：測試 id 只用編號，不把 token 形狀的字串印進測試輸出與 CI log。"""
    return {"argvalues": params, "ids": [f"{kind}{index}" for index in range(len(params))]}


_T = HEX_TOKEN


@pytest.mark.parametrize(("path", "shown"), **_numbered("path", [
    ("/healthz", "/healthz"),
    ("/api/profiles?limit=5", "/api/profiles?limit=5"),
    ("/assets/index-3f9c1e.js", "/assets/index-3f9c1e.js"),
    ("/api/logs/search?q=%E4%B8%AD%E6%96%87&limit=5", "/api/logs/search?q=%E4%B8%AD%E6%96%87&limit=5"),
    ("/api/x?debug", "/api/x?debug"),
    ("/x/" + _T, "/x/" + _T),  # 不是 /private_ 網址：不在遮罩範圍（與 8297aa4 相同）
    (f"/private_{_T}/mcp", "/private_***/mcp"),
    (f"/private_{_T}", "/private_***"),
    (f"/private_{_T}/sse", "/private_***/sse"),
    (f"/private_{_T}/mcp/", "/private_***/mcp/"),
    (f"/private_{_T}/messages/?session_id=0b5e1d9c4c2f4c8e", "/private_***/messages/?session_id=0b5e1d9c4c2f4c8e"),
    (f"/private_{_T}?x=1", "/private_***?x=1"),
    (f"/private_{_T}/mcp?a=1&b=2", "/private_***/mcp?a=1&b=2"),
    ("/private_", "/private_"),
    ("/private_/", "/private_/"),
]))
def test_normal_access_paths_stay_readable(access_sinks, path, shown):
    _log_access(access_sinks.logger, path)
    stdout, ring = access_sinks.read()
    assert stdout == f'INFO:     127.0.0.1:5555 - "POST {shown} HTTP/1.1" 403 Forbidden\n'
    assert ring == ([f'127.0.0.1:5555 - "POST {shown} HTTP/1.1" 403'] if access_sinks.ring_stores_access else [])


@pytest.mark.parametrize(("text", "expected"), **_numbered("text", [
    # 一般 log 訊息（不是 access path）：/private_ 之後的字面空白仍是網址的結尾，後面的字不受影響
    ("GET /private_ HTTP/1.1", "GET /private_ HTTP/1.1"),
    ("route /private_ prefix", "route /private_ prefix"),
    (f"proxy failed for /private_{_T}/mcp: timeout", "proxy failed for /private_***/mcp: timeout"),
    (f'"GET /private_{_T}?x=1 HTTP/1.1"', '"GET /private_***?x=1 HTTP/1.1"'),
    (f"[mcp-server] GET /private_%20{_T}/mcp done", "[mcp-server] GET /private_***/mcp done"),
]))
def test_other_log_messages_keep_their_shape(text, expected):
    assert lr.redact(text) == expected


# ---------------------------------------------------------------------------
# 病態長路徑：單次遮罩的時間上限、沒有每段一次的 Python 工作（N7 的區段規則不能讓 f-1／r2co-1／G5 的 DoS 防護退步）
# ---------------------------------------------------------------------------


def pathological_paths(n: int) -> dict[str, str]:
    """n ≈ 6 萬：題目要求的「6 萬個 %20」與「&= 交錯」，以及區段規則新加的跳過、裸 query 判斷各自的最壞形狀。"""
    return {
        "60k %20 then token": "/private_" + "%20" * n + _T + "/mcp",
        "40k %20 then token (under 128KiB)": "/private_" + "%20" * (n * 2 // 3) + _T + "/mcp",
        "%20 and &= interleaved": "/private_" + "%20&=" * (n // 2) + _T,
        "token then 30k &=": "/private_" + _T + "/mcp?" + "&=" * (n // 2),
        "bare query after 30k &=": "/private_x/mcp?" + "&=" * (n // 2) + "&" + _T,
        "bare query 30k a&": "/private_x/mcp?" + "a&" * (n // 2),
        "60k ? after prefix": "/private_" + "?" * n + _T,
        "60k / after prefix": "/private_" + "/" * n + _T,
        "20k %3F after prefix": "/private_" + "%3F" * (n // 3) + _T,
        "repeated /private_%20": "/private_%20" * (n // 12),
        "repeated /private_x?a=1": "/private_x?a=1" * (n // 14),
        "repeated masked ?": "/private_***/x?" * (n // 15),
        "repeated masked ?a=1/": "/private_***/x?a=1" * (n // 18),
        "%25 chain prefix": "/%" + "25" * n + "70rivate_" + _T,
        "%25 chain region": "/private_%" + "25" * n + "20" + _T,
        "/x? then 30k &=": "/x?" + "&=" * (n // 2),
    }


_N = 60_000


def _best_redact(text: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        lr.redact(text)
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("name", list(pathological_paths(1)))
def test_pathological_path_single_redaction_time_is_bounded(name):
    """單次 redact()（最多 128 KiB）在 50 ms 內（取 3 次最小值，抗 CI 雜訊）；本機實測見 PR 說明。"""
    text = pathological_paths(_N)[name]
    assert _best_redact(text) < 0.05, name
    out = lr.redact(text)
    assert _fragment(out, _T) is None, (name, out[:200])


@pytest.mark.parametrize("name", list(pathological_paths(1)))
def test_pathological_access_record_cost_is_bounded(access_sinks, name):
    """完整 uvicorn.access 紀錄路徑（record factory、filter、AccessFormatter、兩個出口）每筆 20 ms 內，行長有上界。"""
    path = pathological_paths(_N)[name]
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        _log_access(access_sinks.logger, path)
        best = min(best, time.perf_counter() - start)
    stdout, ring = access_sinks.read()
    assert best < 0.02, f"{name}: {best * 1000:.2f} ms"
    assert len(stdout.splitlines()) == 3 and len(ring) == (3 if access_sinks.ring_stores_access else 0)
    for line in stdout.splitlines() + ring:
        assert len(line) < 2300 and _fragment(line, _T) is None, (name, line[:200])


def _python_lines(fn, *args) -> int:
    """fn 執行期間 log_redaction.py／urllib/parse.py 的 Python line event 數（每段一次的迴圈會與段數成正比）。"""
    count = 0
    watched = ("log_redaction.py", "urllib/parse.py")

    def local(frame, event, arg):
        nonlocal count
        if event == "line":
            count += 1
        return local

    def tracer(frame, event, arg):
        if event == "call" and frame.f_code.co_filename.endswith(watched):
            return local
        return None

    old = sys.gettrace()
    sys.settrace(tracer)
    try:
        fn(*args)
    finally:
        sys.settrace(old)
    return count


@pytest.mark.parametrize("name", list(pathological_paths(1)))
def test_pathological_path_has_no_per_segment_python_work(name):
    small = _python_lines(lr.redact, pathological_paths(_N // 16)[name])
    big = _python_lines(lr.redact, pathological_paths(_N)[name])
    assert big < 1500, f"{name}: {big} python lines"
    assert big <= small + 400, f"{name}: python lines grow with input: {small} -> {big}"


# ---------------------------------------------------------------------------
# 與 8297aa4 的差分 fuzz：8297aa4 遮掉的秘密，新版一定也遮掉（8297aa4 的兩條規則凍結在這裡）
# ---------------------------------------------------------------------------

_OLD_PATH_RE = re.compile(r"(/private_)[^/\s?#\"']+")
_OLD_QUERY_RE = re.compile(r"([?&](?:access_)?token=)[^&#\s\"']+", re.IGNORECASE)


def redact_8297aa4(text: str) -> str:
    if not text or not ("/private_" in text or "token=" in text.lower()):
        return text
    return _OLD_QUERY_RE.sub(r"\1***", _OLD_PATH_RE.sub(r"\1***", text))


SECRET = "QZJWXKQZJWXK"  # 合成值；字母刻意避開遮罩標記與佔位字串會用到的字母

_SEPARATORS = ["", " ", "%20", "%2520", "%22", "%27", "'", '"', "%3F", "?", "%23", "#", "%2F", "/", "//", "%5C", "\\",
               "%C2%A0", "%E3%80%80", "%E2%80%80", "%0A", "%09", "%26", "&", "%3B", ";", "%3D", "=", "%25", "%",
               "%3C", "%E2%80%9C", "*", "***", "+", "~", "\ufffd", "\u3000", "%zz", "%2",
               # 十六進位數字本身也編碼、多輪解碼後才出現的 ?（前四個）與 &（獨立審查 2026-11-13）
               "%%33%46", "%3%46", "%25%33%46", "%2%353F", "%%32%36"]
_PREFIXES = ["/private_", "//private_", "/p%72ivate_", "%2Fprivate_", "/%2570rivate_", "\\private_", "/private%5F",
             "/PRIVATE_", "/private\\_", "/p\ufffdrivate_"]
_TAILS = ["", "/mcp", "/mcp?x=1", "/messages/?session_id=abc", "?a=1&token=" + SECRET, " HTTP/1.1", "%20/mcp",
          "&x=1", ";y", "/sse?" + SECRET, "?" + SECRET, "/x?to/ken=" + SECRET, "/x? token=" + SECRET]


def _fuzz_text(r: random.Random) -> str:
    parts = [r.choice(["", "GET ", "x /a", "/api?q=1&"]), r.choice(_PREFIXES)]
    for _ in range(r.randint(0, 3)):
        parts.append(r.choice(_SEPARATORS))
    if r.random() < 0.8:
        parts.append(SECRET if r.random() < 0.6 else SECRET[:6] + r.choice(_SEPARATORS) + SECRET[6:])
    for _ in range(r.randint(0, 2)):
        parts.append(r.choice(_SEPARATORS))
    parts.append(r.choice(_TAILS))
    if r.random() < 0.3:
        parts.append(r.choice(_TAILS))
    return "".join(parts)


def test_differential_fuzz_never_shows_what_8297aa4_masked():
    r = random.Random(20261113)
    for _ in range(30_000):
        text = _fuzz_text(r)
        old, new = redact_8297aa4(text), lr.redact(text)
        for piece in (SECRET, SECRET[:6], SECRET[6:]):
            if piece in text and piece not in old:
                assert piece not in new, (piece, text, new)
        assert lr.redact(new) == new, (text, new)


@pytest.mark.parametrize("text", **_numbered("lead", [
    # 舊版（先解碼、先區段後鍵名）遮得到的鍵名值：區段規則不可吞掉它的 lead 或讓它少遮（修正過程中差分 fuzz 找到的反例）
    "/private_x%3f/token=" + SECRET,
    "/private_x%3F to/ken=" + SECRET,
    "/private_?to/ken=" + SECRET,
    "/private_x%20&token=" + SECRET + "/y",
    "/private_x%20%26token=" + SECRET + "/y",
    "/private_x%3Ftoken%3D" + SECRET[:6] + "/" + SECRET[6:],
    "/private_x/mcp?a&to ken=" + SECRET,
    "GET /p%5Crivate_" + SECRET + ";to.ken=" + SECRET + "%3Fto%20ken=" + SECRET + "/x",
    "&/private_%09to.ken=" + SECRET + "%3F?" + SECRET,
    "/private_x/mcp?" + SECRET + " to/ken=" + SECRET,
    # 十六進位數字本身也編碼，多輪解碼後才出現 ? & =（%3%46、%%33%46、%25%33%46、%2%353F、%%33F → %3F → ?；
    # %%32%36、%25%32%36 → %26 → &；%%33%44 → %3D → =）。區段規則不可把它們當成一般字元而連鍵名的 lead 一起遮掉，
    # e0a80d6 這些都遮得到（獨立審查 2026-11-13 找到的反例；/x?u=… 是 access log 原樣保留的 query）
    "/private_x%3%46to/ken%3D" + SECRET[:6] + "/" + SECRET[6:],
    "?/private_%2%3520%25%32%36to ken=" + SECRET + "/sse",
    "/x?u=/private_ab12%3%46to/ken%3D" + SECRET[:6] + "/" + SECRET[6:],
    "/private_x%%33%46token%%33%44" + SECRET[:6] + "/" + SECRET[6:],
    "/private_x%25%33%46token%25%33%44" + SECRET[:6] + "/" + SECRET[6:],
    "/private_x%2%353Fto/ken=" + SECRET[:6] + "/" + SECRET[6:],
    "/private_x%%33F to/ken=" + SECRET[:6] + "/" + SECRET[6:],
    "/private_x%2520%%32%36to ken=" + SECRET + "/sse",
]))
def test_key_leads_inside_a_private_segment_are_still_used(text):
    out = lr.redact(text)
    assert SECRET[:6] not in out and SECRET[6:] not in out, out
    assert lr.redact(out) == out


@pytest.mark.parametrize("text", **_numbered("cascade", [
    # 遮罩彼此連鎖、第二次遮罩才會再改的寫法（差分 fuzz 找到）：第一次就要遮到底，redact(redact(x)) == redact(x)
    "/p?a=/private_%3FtOKen" + SECRET + "/private_?/",
    ";/PRIVATE_%20&&?" + SECRET[:6] + "%3C" + SECRET[6:] + " to/ken=" + SECRET + "/to/ken=" + SECRET,
    'x /a/p%5Crivate_"%E2%80%9C#' + SECRET[:6] + "%25" + SECRET[6:] + "%%253D%E3%80%80&token=" + SECRET,
    "GET //private_%2520*#" + SECRET[:6] + "%25" + SECRET[6:] + "& to/ken=" + SECRET,
    "?/private_#\\" + SECRET + "//%23 to/ken=" + SECRET,
    ';/private_"=#' + SECRET + "%2%74oken/sse?" + SECRET,
]))
def test_cascading_masks_are_idempotent(text):
    once = lr.redact(text)
    assert lr.redact(once) == once, (once, lr.redact(once))
    assert SECRET[:6] not in once and SECRET[6:] not in once, once


# ---------------------------------------------------------------------------
# 十六進位數字本身也編碼、多輪解碼後才出現的 ? & ; =（獨立審查 2026-11-13）：``%%33%46``、``%3%46``、``%25%33%46``、
# ``%2%353F`` 多輪解碼後是 ``?``，``%%32%36`` 是 ``&``。``_STRICT_PATH_SEGMENT_RE`` 原本只把 ``%(25)*3F／26／3B／3D`` 當成
# 編碼過的 ? & ; =，看不到這類寫法：同一行有憑證鍵名時區段照樣在原本的拼法上遮掉、連鍵名的 lead 一起吞掉，
# e0a80d6 遮得到的值外洩（access log 的 path 經 quote(unquote()) 變成 ``%253F``，看得到；原樣保留的 query 看不到）。
# ---------------------------------------------------------------------------

# 區段內容的零件：% 與十六進位數字的各種拆法。上面的寫法都是幾個零件接起來（``%%33%46``＝% %33 %46、``%3%46``＝%3 %46、
# ``%25%33%46``＝%25 %33 %46、``%2%353F``＝%2 %35 3F、``%%32%36``＝% %32 %36），? & ; = 的十六進位碼（3F、26、3B、3D）
# 與它們的各位數（%33、%46、%32、%36、%42、%44）也都在裡面；``25``、``2`` 讓 25 串更長的寫法（``%2525%33F``）也組得出來。
# lookahead 的 ``(?:25)*+`` 若會回溯，``%25%33F``、``%2525%33F`` 都會被當成 HH＝25 的單位而放行，這裡會失敗。
_ESCAPE_PIECES = ["%", "%2", "%25", "%3", "%32", "%33", "%35", "%36", "%42", "%44", "%46", "%2F",
                  "3F", "3f", "26", "3B", "3D", "25", "2", "5", "F", "x"]


def _decode_to_fixpoint(text: str) -> str | None:
    """反覆 ``unquote`` 到不再改變；不收斂就 None（redact() 那時整句 fail closed）。"""
    for _ in range(4 * lr.MAX_DECODE_ROUNDS):
        decoded = unquote(text, errors="replace")
        if decoded == text:
            return text
        text = decoded
    return None


def _check_strict_segment(body: str) -> None:
    text = f"/private_{body}/mcp"
    masked = lr._PATH_SEGMENT_RE.sub(r"\1" + lr.MASK, text)
    if masked == text or lr._STRICT_PATH_SEGMENT_RE.sub(r"\1" + lr.MASK, text) != masked:
        return  # 區段沒遮，或兩次結果不同（有鍵名時整句 fail closed）
    decoded = _decode_to_fixpoint(text)
    assert decoded is None or not set(decoded) & set("?&;="), (body, decoded)


def test_strict_segment_check_sees_separators_that_appear_in_any_decoding_round():
    """兩次 ``sub`` 結果相同（區段會在原本的拼法上整段遮掉）時，區段整串解碼到底也不可出現 ? & ; =。

    零件 3 個以內窮舉，再隨機組合較長的序列。
    """
    for count in range(1, 4):
        for pieces in itertools.product(_ESCAPE_PIECES, repeat=count):
            _check_strict_segment("".join(pieces))
    r = random.Random(20261113)
    for _ in range(30_000):
        _check_strict_segment("".join(r.choice(_ESCAPE_PIECES) for _ in range(r.randint(4, 10))))


def redact_decoded_first(text: str) -> str:
    """e0a80d6 的 ``redact()`` 順序（凍結在這裡）：整串解碼到底（無法判斷就整句換掉），之後先遮 ``/private_`` 區段、後遮鍵名。

    判定與 pattern 沿用本模組：``_has_candidate``、``_decode_or_fail``（與 e0a80d6 的解碼判定逐字相同）、鍵名 pattern 都沒有改；
    ``_PATH_TOKEN_RE`` 在解碼後的字串上與 e0a80d6 相同（前綴的任意層編碼只對還沒解碼的字串有差別）。輸入都很短，不涉及截斷。
    """
    if not text or not lr._has_candidate(text):
        return text
    decoded = lr._decode_or_fail(text)
    if decoded is None:
        return lr.AMBIGUOUS
    low = decoded.lower()
    cleaned = decoded
    if "_" in decoded or "%5f" in low:
        cleaned = lr._PATH_TOKEN_RE.sub(r"\1" + lr.MASK, cleaned)
    if lr._keyed(decoded, low):
        cleaned = lr._mask_keys(cleaned)
    return text if cleaned == decoded else cleaned


# 合成值：沒有十六進位字母（不會跟在 % 後面變成跳脫）、沒有鍵名的字母，任何 6 個連續字元的片段在值裡都只出現一次。
_SPELL_SECRET = "QZJWXYHGLMUW"
_SPELL_WINDOWS = [_SPELL_SECRET[i:i + 6] for i in range(len(_SPELL_SECRET) - 5)]


def _spell(char: str, r: random.Random) -> str:
    """一個 ASCII 字元在紀錄裡的各種拼法：字面、``%HH``、``%25HH``、``%2525HH``，以及十六進位數字本身也編碼、多輪解碼後
    才還原的 ``%%33%46``、``%%33F``、``%3%46``、``%25%33%46``、``%2%353F``（以 ``?`` 為例）。"""
    hh = f"{ord(char):02X}"
    d1, d2 = (f"%{ord(digit):02X}" for digit in hh)
    form = r.choice([char, char, "%" + hh, "%25" + hh, "%2525" + hh, "%" + d1 + d2, "%" + d1 + hh[1],
                     "%" + hh[0] + d2, "%25" + d1 + d2, "%2%35" + hh])
    return form.lower() if form != char and r.random() < 0.3 else form


def _spelled_fuzz_text(r: random.Random) -> str:
    s = _SPELL_SECRET
    parts = [r.choice(["", "GET ", "x /a", "?", "&", "/api?q=1&", "/x?u="]), "/private_"]
    for _ in range(r.randint(0, 2)):
        parts.append(_spell(r.choice(" ?&;=/x\"'"), r))
    if r.random() < 0.6:
        cut = r.randint(1, len(s) - 1)
        parts.append(s[:cut] + "".join(_spell(r.choice(" ?&;=/x"), r) for _ in range(r.randint(0, 1))) + s[cut:])
    for _ in range(r.randint(0, 2)):
        parts.append(_spell(r.choice(" ?&;=x"), r))
    if r.random() < 0.8:
        parts.append(_spell(r.choice("?&; /"), r))
        key = r.choice(["token", "access_token", "to/ken", "to ken", "to.ken"])
        parts.append("".join(_spell(char, r) if r.random() < 0.15 else char for char in key))
        parts.append(_spell("=", r))
        cut = r.randint(1, len(s) - 1)
        parts.append(s[:cut] + r.choice(["", "/", "%20", "/x/", "?"]) + s[cut:])
    parts.append(r.choice(["", "/mcp", " HTTP/1.1", "?x=1", "&x=1", "/sse", " done"]))
    return "".join(parts)


def _windows(text: str) -> set[str]:
    return {window for window in _SPELL_WINDOWS if window in text}


def test_differential_fuzz_never_shows_what_decoded_first_order_masked():
    """e0a80d6 的順序遮掉的秘密片段，新版一定也遮掉。分隔字元、鍵名字母與 ``=`` 都用上多輪解碼才還原的寫法——只有
    ``%3F``、``%253F`` 這類完整跳脫的輸入找不到這一類（PR #144 第一版的差分 fuzz 就是這樣漏掉的）。"""
    r = random.Random(20261114)
    for _ in range(20_000):
        text = _spelled_fuzz_text(r)
        seen = _windows(text)
        if not seen:
            continue
        old, new = redact_decoded_first(text), lr.redact(text)
        lost = (seen - _windows(old)) & _windows(new)
        assert not lost, (text, old, new)
        assert lr.redact(new) == new, (text, new)


@pytest.mark.parametrize("target", **_numbered("target", [
    # query 原樣進 access log：多輪解碼才出現的 ? 夾在 /private_ 區段裡，後面的 to/ken%3D 是鍵名
    "/x?u=/private_ab12%3%46to/ken%3D" + SECRET[:6] + "/" + SECRET[6:],
    "/x?u=/private_ab12%%33%46token%%33%44" + SECRET[:6] + "/" + SECRET[6:],
    # path 經 quote(unquote())：%3%46 在紀錄裡是 %253F（多一層編碼的 ?），一樣不可留下值
    "/private_ab12%3%46to/ken%3D" + SECRET[:6] + "/" + SECRET[6:],
]))
def test_multi_round_encoded_separator_does_not_leak_into_access_log(access_sinks, target):
    path = _uvicorn_access_path(target)
    assert path is not None
    _log_access(access_sinks.logger, path)
    stdout, ring = access_sinks.read()
    lines = stdout.splitlines()
    assert len(lines) == 1 and len(ring) == (1 if access_sinks.ring_stores_access else 0), (lines, ring)
    for text in lines + ring:
        assert SECRET[:6] not in text and SECRET[6:] not in text, text
    shown = _STDOUT_LINE.fullmatch(lines[0])
    assert shown and ring == ([f'127.0.0.1:5555 - "POST {shown.group(1)} HTTP/1.1" 403'] if ring else [])
