"""URL query 裡的憑證值不進 log（容器 stdout 與 Logs 頁 ring buffer 同一份遮罩）。所有 token、密碼都是合成值。

1. **#144 二次審查建議 1**：``?token=``／``access_token=`` 的值前面或中間先接編碼過的分隔字元時
   （``/api/logs/stream?token=%20<JWT>``、``%22<JWT>%22``、``%E3%80%80<JWT>``…），443e95a 先把整串解碼再比對，值停在
   「解碼出來」的空白、引號上，整個管理頁 JWT 留在容器 stdout（8297aa4 只比對字面，會遮）。這和 N7 是同一型，只是對象換成
   query 的值；這裡把 N7 的 97 種寫法（mp-verify-n8n-20261113/harness/cases.py，由 test_log_redaction_private_separators 的
   ``build_targets`` 產生）原樣搬到 query 值上，三種 token 形狀（64 hex、URL-safe、管理頁 JWT）、三種網址（Logs 頁、
   ``/private_`` 網址、一般網址），經兩個出口都不可留下 8 個字元以上的片段。
2. **上線前重測 info**：非 token 鍵名的常見憑證參數（``?api_key=``、``?key=``、``?secret=``、``?password=``、``?sig=``…）
   443e95a 原樣留在 access log。鍵名判準見 ``log_redaction.CREDENTIAL_PARAM_NAMES``：大小寫、底線／連字號／無分隔、
   任意層編碼的寫法都算，只認完整的參數名稱（``mytoken``、``token_type``、``keys``、``key_id``、``signature_version`` 不遮）。
3. **#144 二次審查建議 2**：同一行有鍵名，加上上萬個任意層編碼前綴的 ``/private_`` 區段；以及新規則各自的最壞形狀。
   單次 ``redact()`` 的 CPU 時間（絕對上限 250 ms、輸入加倍不超過 2.8 倍）、完整 access 紀錄 20 ms 內，沒有每段一次的 Python 工作。
4. 與凍結的 443e95a 模組（``tests/fixtures/log_redaction_443e95a.py``，逐字複製）做差分 fuzz：舊版遮掉的秘密片段，新版
   一定也遮掉；結果冪等。開發過程中 fuzz 找到的反例另外列成固定測試。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import logging
import random
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

from mcp_admin_core import log_redaction as lr
# 97 種寫法、兩個出口（容器 stdout 與 Logs 頁 ring buffer）的 fixture、真 uvicorn 的工具沿用 N7 的測試，確保是同一份矩陣。
from test_log_redaction_private_separators import (  # noqa: F401 — access_sinks／app_logger_sinks 是 pytest fixture
    TOKENS,
    _forbidden_app,
    _python_lines,
    _send_raw,
    _uvicorn_access_path,
    access_sinks,
    app_logger_sinks,
    build_targets,
)

MIN_FRAGMENT = 8


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# 合成的管理頁 JWT 形狀（header.payload.signature），簽章是固定字串的 sha256，不是任何服務的憑證。
JWT_TOKEN = ".".join((_b64(b'{"alg":"HS256","typ":"JWT"}'), _b64(b'{"sub":"admin","exp":4102444800}'),
                      _b64(hashlib.sha256(b"woow-mcp synthetic admin jwt for query redaction tests").digest())))
QUERY_TOKENS = {**TOKENS, "jwt": JWT_TOKEN}


def _fragment(text: str, token: str) -> str | None:
    for start in range(len(token) - MIN_FRAGMENT + 1):
        piece = token[start:start + MIN_FRAGMENT]
        if piece in text:
            return piece
    return None


def _numbered(kind: str, params: list) -> dict:
    """參數裡有合成憑證：測試 id 只用編號，不把憑證形狀的字串印進測試輸出與 CI log。"""
    return {"argvalues": params, "ids": [f"{kind}{index}" for index in range(len(params))]}


# ---------------------------------------------------------------------------
# 1. N7 的 97 種寫法搬到 query 值上
# ---------------------------------------------------------------------------


def query_value_forms(token: str) -> dict[str, str]:
    """case 名稱 → query 值的寫法：``/private_<寫法>/mcp`` 的 ``<寫法>`` 直接當成 ``?token=<寫法>``。

    原本就與 query 有關或只差在前綴的 4 種（``query_token_param``、``query_bare_token``、``double_leading_slash``、
    ``canonical_no_suffix``）在 query 值上都是 token 本身。
    """
    forms = {}
    for case, target in build_targets(token).items():
        if case in ("query_token_param", "query_bare_token", "double_leading_slash", "canonical_no_suffix"):
            forms[case] = token
            continue
        body = target[len("/private_"):]
        forms[case] = body[:-len("/mcp")] if body.endswith("/mcp") else body
    return forms


QUERY_CASES = list(query_value_forms(JWT_TOKEN))
# 解碼後是控制字元或新的 ``#``：與 N7 相同，整段 path 換成「無法判斷」（不是外洩，刻意不放寬）。
AMBIGUOUS_QUERY_CASES = {"enc_first_tab", "enc_first_lf", "enc_first_cr", "enc_first_vt", "enc_first_ff",
                         "enc_first_fs_1c", "enc_first_us_1f", "enc_first_hash"}
TOKEN_KEYS = ("token", "access_token")
QUERY_BASES = ("/api/logs/stream", "/private_0b5e1d9c4c2f4c8e/mcp", "/x/y")
QUERY_TAILS = ("", "&tail=1")


def test_query_value_matrix_is_the_reviewed_97_cases():
    assert len(QUERY_CASES) == 97
    for token in QUERY_TOKENS.values():
        forms = query_value_forms(token)
        assert all(value.isascii() for value in forms.values())
        assert forms["enc_first_space"] == "%20" + token and forms["wrap_dquote_encoded"] == "%22" + token + "%22"


def _shown_base(base: str) -> str:
    return "/private_***/mcp" if base.startswith("/private_") else base


def _query_targets(case: str, token: str, keys=TOKEN_KEYS):
    value = query_value_forms(token)[case]
    for key in keys:
        for base in QUERY_BASES:
            for tail in QUERY_TAILS:
                yield key, base, tail, f"{base}?{key}={value}{tail}"


def _check_shown(case: str, token: str, key: str, base: str, shown: str) -> None:
    assert _fragment(shown, token) is None, (case, key, base, shown)
    if case in AMBIGUOUS_QUERY_CASES or (case == "enc_first_percent" and not re.fullmatch(r"[0-9A-Fa-f]{2}", token[:2])):
        assert shown == lr.AMBIGUOUS, (case, shown)
        return
    # 仍可讀：網址本身照舊，只有值（或 /private_ 網址的整段裸 query）換成 ***
    assert shown.startswith(_shown_base(base) + "?") and lr.AMBIGUOUS not in shown, (case, shown)
    # 值以編碼過的 & 開頭（enc_first_amp）：解碼後值是空的、token 成了下一個沒有 = 的參數，那個參數遮成 ***
    assert f"{key}=***" in shown or f"{key}=&***" in shown or shown.startswith("/private_***/mcp?***"), (case, shown)


_ACCESS_LINE = re.compile(r'INFO:     127\.0\.0\.1:5555 - "GET (.*) HTTP/1\.1" 401 Unauthorized')


def _log_access(logger: logging.Logger, path: str) -> None:
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", path, "1.1", 401)


@pytest.mark.parametrize("token_kind", list(QUERY_TOKENS))
@pytest.mark.parametrize("case", QUERY_CASES)
def test_query_value_forms_are_masked_in_stdout_and_ring_buffer(access_sinks, case, token_kind):
    token = QUERY_TOKENS[token_kind]
    for key, base, tail, target in _query_targets(case, token):
        path = _uvicorn_access_path(target)
        if path is None:
            # 只有字面空白（lit_first_space）：httptools 拒絕，真的 uvicorn 回 400、沒有 access log（見真 uvicorn 的測試）
            assert case == "lit_first_space"
            continue
        access_sinks.reset()
        _log_access(access_sinks.logger, path)
        stdout, ring = access_sinks.read()
        lines = stdout.splitlines()
        assert len(lines) == 1 and len(ring) == (1 if access_sinks.ring_stores_access else 0), (lines, ring)
        shown = _ACCESS_LINE.fullmatch(lines[0])
        assert shown, lines[0]
        assert ring == ([f'127.0.0.1:5555 - "GET {shown.group(1)} HTTP/1.1" 401'] if ring else [])  # 同一份遮罩
        _check_shown(case, token, key, base, shown.group(1))


@pytest.mark.parametrize("token_kind", list(QUERY_TOKENS))
@pytest.mark.parametrize("case", QUERY_CASES)
def test_query_value_forms_are_masked_in_app_log_lines(app_logger_sinks, case, token_kind):
    logger, read = app_logger_sinks
    token = QUERY_TOKENS[token_kind]
    count = 0
    for key, base, tail, target in _query_targets(case, token):
        path = _uvicorn_access_path(target)
        if path is None:
            continue
        logger.warning("proxy %s %s failed: %s", "GET", path, "upstream timeout")
        count += 1
        stdout, ring = read()
        assert len(ring) == count
        assert stdout.splitlines()[-1] == f"WARNING mcp_admin_core.n7_log_redaction_test {ring[-1]}"  # 同一份遮罩
        assert _fragment(stdout, token) is None and _fragment(ring[-1], token) is None, (case, ring[-1])


@pytest.mark.parametrize("token_kind", list(QUERY_TOKENS))
@pytest.mark.parametrize("case", QUERY_CASES)
def test_query_value_forms_redaction_is_idempotent(case, token_kind):
    token = QUERY_TOKENS[token_kind]
    for key, base, tail, target in _query_targets(case, token, keys=TOKEN_KEYS + ("api_key", "sig")):
        path = _uvicorn_access_path(target)
        if path is None:
            # 字面空白（lit_first_space）：真的 uvicorn 回 400、不寫 access log；一般訊息裡 ``=`` 後面的字面空白就是網址的
            # 結尾（與 443e95a 相同，見 CLAUDE.md 已知限制），不在這個矩陣的範圍
            continue
        texts = [path, f'127.0.0.1:5555 - "GET {path} HTTP/1.1" 401', target]
        for text in texts:
            once = lr.redact(text)
            assert _fragment(once, token) is None, (case, key, once)
            assert lr.redact(once) == once, (case, key, once, lr.redact(once))
        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                   ("127.0.0.1:5555", "GET", path, "1.1", 401), None)
        lr.scrub_record(record)
        first = record.args
        record.args = tuple(first)  # 新的 tuple：指紋對不上，一定會再遮一次
        lr.scrub_record(record)
        assert record.args == first


@pytest.mark.parametrize("token_kind", list(QUERY_TOKENS))
@pytest.mark.parametrize("case", QUERY_CASES)
def test_query_value_forms_with_credential_param_names_are_masked(access_sinks, case, token_kind):
    """同一套 97 種寫法放在非 token 的憑證鍵名上（443e95a 這些全部原樣留下）。"""
    token = QUERY_TOKENS[token_kind]
    for key, base, tail, target in _query_targets(case, token, keys=("api_key", "Client-Secret", "sig", "password")):
        path = _uvicorn_access_path(target)
        if path is None:
            continue
        access_sinks.reset()
        _log_access(access_sinks.logger, path)
        stdout, ring = access_sinks.read()
        shown = _ACCESS_LINE.fullmatch(stdout.splitlines()[0])
        assert shown and ring == ([f'127.0.0.1:5555 - "GET {shown.group(1)} HTTP/1.1" 401'] if ring else [])
        _check_shown(case, token, key, base, shown.group(1))


@pytest.mark.parametrize(("target", "shown"), **_numbered("review", [
    # #144 二次審查點名的三種寫法（443e95a 都留下整個 JWT），以及值的前後、中間各夾一個編碼分隔字元
    (f"/api/logs/stream?token=%20{JWT_TOKEN}", "/api/logs/stream?token=***"),
    (f"/api/logs/stream?token=%22{JWT_TOKEN}%22", "/api/logs/stream?token=***"),
    (f"/api/logs/stream?token=%E3%80%80{JWT_TOKEN}", "/api/logs/stream?token=***"),
    (f"/api/logs/stream?token={JWT_TOKEN[:20]}%20{JWT_TOKEN[20:]}&tail=50", "/api/logs/stream?token=***&tail=50"),
    (f"/api/logs/stream?access_token=%C2%A0{JWT_TOKEN}%27", "/api/logs/stream?access_token=***"),
    (f"/api/logs/stream?tail=50&token=%2520{JWT_TOKEN}", "/api/logs/stream?tail=50&token=***"),
    # 正常的 Logs 頁網址：與 443e95a 相同
    (f"/api/logs/stream?token={JWT_TOKEN}", "/api/logs/stream?token=***"),
    (f"/api/logs/search?q=%E4%B8%AD&limit=5&token={JWT_TOKEN}", "/api/logs/search?q=%E4%B8%AD&limit=5&token=***"),
]))
def test_review_examples_keep_a_readable_access_line(access_sinks, target, shown):
    path = _uvicorn_access_path(target)
    _log_access(access_sinks.logger, path)
    stdout, ring = access_sinks.read()
    assert stdout == f'INFO:     127.0.0.1:5555 - "GET {shown} HTTP/1.1" 401 Unauthorized\n'
    assert ring == ([f'127.0.0.1:5555 - "GET {shown} HTTP/1.1" 401'] if access_sinks.ring_stores_access else [])


def test_real_uvicorn_access_log_masks_query_value_forms(access_sinks):
    """真的 uvicorn（httptools）收原始請求列：Logs 頁 JWT 與 /private_ 網址的 query 值，97 種寫法 × 兩種鍵名。"""
    import uvicorn

    error_logger = logging.getLogger("uvicorn.error")
    saved_error = (error_logger.propagate, list(error_logger.handlers))
    error_logger.propagate, error_logger.handlers = False, [logging.NullHandler()]
    config = uvicorn.Config(_forbidden_app, http="httptools", loop="asyncio", lifespan="off", log_config=None,
                            access_log=True)
    server = uvicorn.Server(config)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    sent = []
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        for case in QUERY_CASES:
            value = query_value_forms(JWT_TOKEN)[case]
            for key in TOKEN_KEYS:
                for base in ("/api/logs/stream", "/private_0b5e1d9c4c2f4c8e/mcp"):
                    target = f"{base}?{key}={value}"
                    sent.append((case, key, base, *_send_raw(port, target)))
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        error_logger.propagate, error_logger.handlers = saved_error
    assert not thread.is_alive()
    stdout, ring = access_sinks.read()
    assert _fragment(stdout, JWT_TOKEN) is None, stdout[:500]
    assert _fragment("\n".join(ring), JWT_TOKEN) is None
    line = re.compile(r'127\.0\.0\.1:(\d+) - "POST (.*) HTTP/1\.1"')
    stdout_by_port = {int(m.group(1)): m.group(2) for m in line.finditer(stdout)}
    for case, key, base, local_port, status in sent:
        if case == "lit_first_space":
            assert status == 400 and local_port not in stdout_by_port
            continue
        assert status == 403, (case, status)
        _check_shown(case, JWT_TOKEN, key, base, stdout_by_port[local_port])
    assert len(stdout_by_port) == len(sent) - 2 * len(TOKEN_KEYS)


# ---------------------------------------------------------------------------
# 2. 非 token 鍵名的憑證參數
# ---------------------------------------------------------------------------

SECRET = "QZJWXYHGLMUWQZYJ"  # 合成值：字母不在任何鍵名裡，也不是十六進位數字

# 上線前重測列出的憑證鍵名（api_key、apikey、key、secret、client_secret、password、passwd、pwd、auth、authorization、jwt、
# access_key、signature、sig），加上同類的常見寫法。以「字」列出：字之間可以是底線、連字號或什麼都沒有（api_key、api-key、
# apikey），大小寫不拘。log_redaction.CREDENTIAL_PARAM_NAMES 必須與這份相同（改清單時兩邊與 CLAUDE.md 一起改）。
CREDENTIAL_WORDS = (
    ("api", "key"), ("x", "api", "key"), ("key",), ("secret",), ("client", "secret"), ("secret", "key"),
    ("api", "secret"), ("password",), ("passwd",), ("pwd",), ("auth",), ("authorization",), ("jwt",),
    ("access", "key"), ("signature",), ("sig",), ("private", "key"),
    ("x", "amz", "signature"), ("x", "amz", "security", "token"), ("x", "goog", "signature"),
    ("refresh", "token"), ("id", "token"), ("auth", "token"), ("session", "token"), ("app", "key"),
    ("consumer", "secret"), ("passphrase",),
)


def _name_variants(words: tuple[str, ...]) -> list[str]:
    out = []
    for sep in ("_", "-", ""):
        name = sep.join(words)
        out += [name, name.upper(), "".join(c.upper() if i % 2 else c for i, c in enumerate(name))]
    return sorted(set(out))


ALL_NAME_VARIANTS = sorted({v for words in CREDENTIAL_WORDS for v in _name_variants(words)})


def test_credential_param_name_list_matches_the_module():
    assert tuple(getattr(lr, "CREDENTIAL_PARAM_NAMES", ())) == CREDENTIAL_WORDS
    reported = {"api_key", "apikey", "key", "secret", "client_secret", "password", "passwd", "pwd", "auth",
                "authorization", "jwt", "access_key", "signature", "sig"}
    assert reported <= set(ALL_NAME_VARIANTS)


@pytest.mark.parametrize("name", ALL_NAME_VARIANTS)
def test_credential_param_values_are_masked(name):
    for text, shown in (
        (f"/x?{name}={SECRET}", f"/x?{name}=***"),
        (f"/x?a=1&{name}={SECRET}&b=2", f"/x?a=1&{name}=***&b=2"),
        (f"/x?a=1;{name}={SECRET}", f"/x?a=1;{name}=***"),
        (f"GET /x?{name}={SECRET} HTTP/1.1", f"GET /x?{name}=*** HTTP/1.1"),
        (f"/x?{name}=%20{SECRET}%20&b=2", f"/x?{name}=***&b=2"),
        (f"/x?{name}=%22{SECRET}%22", f"/x?{name}=***"),
    ):
        assert lr.redact(text) == shown, (text, lr.redact(text))
    # 值裡有 / + = 這類 base64 字元、或編碼過的 /（presigned URL 的簽章常見）
    for value in (SECRET[:8] + "/" + SECRET[8:] + "+=", SECRET[:8] + "%2F" + SECRET[8:] + "%2B%3D",
                  "%20" + SECRET[:8] + "%2B" + SECRET[8:] + "%3D%3D"):
        out = lr.redact(f"/x?{name}={value}&b=2")
        assert SECRET[:8] not in out and SECRET[8:] not in out, (name, value, out)
        assert lr.redact(out) == out


@pytest.mark.parametrize("text", **_numbered("encoded", [
    # 鍵名本身被編碼（單層、多層），lead 與 = 被編碼（網址被當成另一個參數的值時常見）
    f"/x?api%5Fkey={SECRET}",
    f"/x?%61pi_key={SECRET}",
    f"/x?api%255Fkey={SECRET}",
    f"/x?API%2DKEY={SECRET}",
    f"/x?%2561pi%255fkey={SECRET}",
    f"/x?s%69g={SECRET}",
    f"/x?u=%2Fapi%3Fapi_key%3D{SECRET}",
    f"/x?u=%2Fapi%3Fapi_key%3D%2520{SECRET}",
    f"/x?u=%252Fapi%253Fpassword%253D{SECRET}",
    f"/x?u=/api%3Fclient_secret%3D{SECRET[:8]}%2520{SECRET[8:]}",
]))
def test_encoded_credential_param_names_are_masked(text):
    out = lr.redact(text)
    assert SECRET[:8] not in out and SECRET[8:] not in out, out
    assert lr.redact(out) == out


@pytest.mark.parametrize("text", [
    "/x?limit=5&page=2&x=1",
    "/api/logs/search?q=token&limit=100&regex=false",
    "/api/logs/stream?tail=50",
    "/x?mytoken=abc&token_type=bearer&session_id=0b5e1d9c4c2f4c8e",
    "/x?keys=1&key_id=2&monkey=3&keyword=4&keyring=5",
    "/x?signature_version=4&auth_mode=basic&authority=x&passwords_changed=1",
    "/x?tokens=1&xtoken=2&token_type=bearer&secrets=3&sigma=4&pwd_hint=5",
    "/api/skills/content?name=apikey",
    "/x?q=password&q2=api_key",
    "/x?api_key",
    "/x?api_key=",
    "/x?a=b#api_key=c",
    "api_key=abc without a lead",
    "/private_***/messages/?session_id=0b5e1d9c4c2f4c8e",
])
def test_ordinary_params_are_not_masked(text):
    assert lr.redact(text) == text


@pytest.mark.parametrize(("path", "shown"), **_numbered("access", [
    (f"/key/info?key=sk-{SECRET}", "/key/info?key=***"),
    (f"/v1/models?api_key={SECRET}&limit=5", "/v1/models?api_key=***&limit=5"),
    (f"/api/x?X-Api-Key={SECRET}", "/api/x?X-Api-Key=***"),
    (f"/upload?X-Amz-Signature={SECRET}&X-Amz-Expires=300", "/upload?X-Amz-Signature=***&X-Amz-Expires=300"),
    (f"/private_{SECRET}/mcp?jwt={SECRET}", "/private_***/mcp?jwt=***"),
    (f"/x?password=%27{SECRET}%27", "/x?password=***"),
    ("/x?limit=5&page=2", "/x?limit=5&page=2"),
]))
def test_credential_params_in_access_log_both_sinks(access_sinks, path, shown):
    _log_access(access_sinks.logger, path)
    stdout, ring = access_sinks.read()
    assert stdout == f'INFO:     127.0.0.1:5555 - "GET {shown} HTTP/1.1" 401 Unauthorized\n'
    assert ring == ([f'127.0.0.1:5555 - "GET {shown} HTTP/1.1" 401'] if access_sinks.ring_stores_access else [])


def test_credential_params_in_child_output_both_sinks(app_logger_sinks):
    """子行程輸出經 McpProcessManager 轉錄（LiteLLM 的 /key/info?key=sk-…，見 apps/litellm/litellm_mcp_admin/launcher.py）。"""
    logger, read = app_logger_sinks
    logger.info("[mcp-server] %s", f'HTTP Request: GET http://127.0.0.1:4000/key/info?key=sk-{SECRET} "HTTP/1.1 200 OK"')
    logger.info("[mcp-server] %s", f"GET /v1/x?apikey={SECRET}&secret={SECRET}&pwd={SECRET}")
    stdout, ring = read()
    assert SECRET not in stdout and SECRET not in "\n".join(ring)
    assert ring == ['[mcp-server] HTTP Request: GET http://127.0.0.1:4000/key/info?key=*** "HTTP/1.1 200 OK"',
                    "[mcp-server] GET /v1/x?apikey=***&secret=***&pwd=***"]


@pytest.mark.parametrize("text", **_numbered("bare", [
    # /private_ 網址的裸 query（有沒有 = 的參數就整段遮）：前面的參數若是已遮的憑證（=***）仍要算成「有 = 的參數」，
    # 否則後面的 token 會留下（開發時差分 fuzz 找到）
    f"/private_x/mcp?api_key=abc&{SECRET}&token=x",
    f"/private_x/mcp?api_key=AbC/PqRs&{SECRET}&token=x",
    f"/private_x/mcp?token=%20abc&{SECRET}",
    f"/private_x/x;api_key=abc&y?{SECRET}",
    f'"/private_ab12/messages/;pass=a/b&XAPIKEY=%3E{SECRET[:8]}%%33fauthorization=%%32%46&{SECRET[8:]}/mcp',
    f"x /private_ab12/messages/?a%55tH=VK%3BTN?{SECRET}%%32%30?secret2=%20?keys=%2F HTTP/1.1",
    f"https://h:8443/pr%%369vate%25%35%46%3D%253Fx_api_key=VK%2FP{SECRET};y",
]))
def test_masked_credentials_do_not_hide_a_bare_query(text):
    out = lr.redact(text)
    assert SECRET[:8] not in out and SECRET[8:] not in out, out
    assert lr.redact(out) == out


@pytest.mark.parametrize("text", **_numbered("lead", [
    # 只有解碼後才認得的前綴（``\%2%35%370rivat…_`` 解碼後是 ``\private_``），它的區段在解碼後停在 token 值裡解碼出來的空白；
    # 值先遮成 *** 的話區段一路吞過 ``&``，把後面 ``&tok e n=`` 的 lead 吃掉、值反而留下（開發時差分 fuzz 找到）
    f"http://h\\%2%35%370rivat%%32565_%645%30B%2526token%3Dd%33%20&tok e n%%33D{SECRET}/z;y",
    f"/p%5Crivate_x&token=%22ab%22&to ken={SECRET}",
    f"/p%5Crivate_x;api_key=%20ab&to.ken={SECRET}",
]))
def test_decoded_only_prefix_keeps_later_key_leads(text):
    out = lr.redact(text)
    assert SECRET[:8] not in out and SECRET[8:] not in out, out
    assert lr.redact(out) == out


@pytest.mark.parametrize("text", **_numbered("idem", [
    # 開發時差分 fuzz 找到的不冪等寫法：第二次遮罩不可再改
    "/Private_%2%353bLYZWUJ?%2520%252Fprivate_j token=3D(&x%3%46\\%252F%%32%3570rivate_%252526",
    f"/a/b\\%%37%30ri%25%37%36at%65_%%32%32%26{SECRET}/sse&x%5Fgoog%5Fsignature=VKPRSITN?a=b&c",
    f'"/api/logs/search?keys=a/b?auTHOrIzAtiOn=VKPRSITN%%3253f?private_key={SECRET};x.token=x%26y/mcp',
    f"x /x?%%33F{SECRET}?PRIVATE-KEY=%27VKPRSITN%27&%;",
    f"url=/api/logs/search?token=%27VKPRSITN%27?aPi%255FSeCReT=%%3253F{SECRET};x&debug",
    f"/x?#f;a%25%32%3563cess_%2574o%25%36%62en='VKPRSITN'&j%7%37t={SECRET}?tail=%20;key_id=",
    f"x /api/logs/search?S%2%3565%2%3563R%%32565%%32574%2%4%34k%25%34%35Y=%3DVKPRSITN&ACCESS%2DTOKEN%25%32%353D'{SECRET}#frag",
    f"GET /x?authorization=VK%%350RSITN;auth=%%32522{SECRET}",
    # 值的結尾是前綴噪音 U+FFFD、後面緊接 \private_：值遮掉之後反斜線就成了前綴起點
    f"?v=1;%%33Bclient-secret=VKPRSITN\ufffd%5Cpriva%25%37%34e_%EF%BB%BF{SECRET}%7C/x/mcp%5Cprivate_***%20/mcp",
    f"/x?api_key=ab\ufffd\\private_{SECRET}&q=1",
]))
def test_cascading_credential_masks_are_idempotent(text):
    once = lr.redact(text)
    assert lr.redact(once) == once, (once, lr.redact(once))
    target = quote(text, safe="/?&;=%")
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5555", "GET", _uvicorn_access_path(target) or text, "1.1", 401), None)
    lr.scrub_record(record)
    first = record.args
    record.args = tuple(first)
    lr.scrub_record(record)
    assert record.args == first


# ---------------------------------------------------------------------------
# 3. 病態形狀：同一行有鍵名＋上萬個任意層編碼前綴的區段；新規則各自的最壞形狀
# ---------------------------------------------------------------------------


def _fill(unit: str, size: int, head: str = "", tail: str = "") -> str:
    return head + unit * max(1, (size - len(head) - len(tail)) // len(unit)) + tail


def pathological_shapes(size: int) -> dict[str, str]:
    """題目要求的形狀（#144 二次審查建議 2）：同一行有鍵名，加上上萬個任意層編碼前綴的 ``/private_`` 區段（128 KiB 約
    9 千～1 萬 1 千個區段）。鍵名有 token、非 token 的憑證鍵名、值前面夾編碼空白、值裡有 ``/``（解碼後才遮）四種。"""
    shapes = {}
    for key in ("?token=abc ", "?api_key=abc ", "?token=%20abc ", "?sig=a/b "):
        shapes.update({
            f"{key!r} + /p%2572ivate_x": _fill("/p%2572ivate_x", size, key),
            f"{key!r} + /%2570%2572%2569vate_x": _fill("/%2570%2572%2569vate_x", size, key),
            f"{key!r} + /%252570rivate_x": _fill("/%252570rivate_x", size, key),
            f"{key!r} + /%%37%30rivate_x": _fill("/%%37%30rivate_x", size, key),
            f"{key!r} + /p\\rivate_x": _fill("/p\\rivate_x", size, key),
            f"/private_x + {key!r} + 15-layer tail": "/private_x" + key + _fill("%" + "25" * 14 + "41", size - 20),
        })
    return shapes


def rule_shapes(size: int) -> dict[str, str]:
    """新規則各自的最壞形狀（上萬個鍵名、值的掃描、裸 query 跨過 ``=***``），大小同 test_log_redaction_linear.py 的 64 KiB。"""
    return {
        "?token=%20a repeated": _fill("?token=%20a", size),
        "?token=&a repeated": _fill("?token=&a", size),
        "&api_key=;a repeated": _fill("&api_key=;a", size),
        "?api_key=%26a repeated": _fill("?api_key=%26a", size),
        "?api_key=a/b repeated": _fill("?api_key=a/b", size),
        ";sig= repeated": _fill(";sig=", size),
        "?x-amz-security-token= repeated": _fill("?x-amz-security-token=", size),
        "%3Fapi%255Fkey%3D repeated": _fill("%3Fapi%255Fkey%3D", size),
        "?%2525…61pi_key= repeated": _fill("?%" + "25" * 20 + "61pi_key=x", size),
        "/private_***/x;api_key=*** repeated": _fill("/private_***/x;api_key=***", size),
        "/private_x/mcp? + &sig=a?b repeated": _fill("&sig=a?b", size, "/private_x/mcp?"),
        "/private_x/mcp? + &= + key": _fill("&=", size, "/private_x/mcp?", "&token=%20a/b"),
        "api_key= + %22 run": "?api_key=" + "%22" * (size // 3 - 10) + "abc",
        "token= + %20 run + /": "?token=" + "%20" * (size // 3 - 10) + "abc/",
        "api_key= + long clean value + /p": "?api_key=" + "a" * (size - 20) + "/p",
    }


_SIZE = 131_072
_RULE_SIZE = 65_536
SHAPES = {**{("segments", name): (pathological_shapes, _SIZE) for name in pathological_shapes(1000)},
          **{("rules", name): (rule_shapes, _RULE_SIZE) for name in rule_shapes(1000)}}


def _shape(key, size=None) -> str:
    build, default = SHAPES[key]
    return build(size or default)[key[1]]


# 量的是這個行程用掉的 CPU 時間（``time.process_time``），不是牆上時間：共用的 CI 節點上別的行程搶 CPU 時，牆上時間會忽大忽小
# （本機 6 個並行的測試加上滿載的機器，同一個形狀的 128 KiB 輸入從 20 ms 量到 100 ms，輸入加倍的比值量到 6），CPU 時間不受影響。
def _best_redact(text: str, runs: int = 5) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.process_time()
        lr.redact(text)
        best = min(best, time.process_time() - start)
    return best


def _best_pair(small_text: str, big_text: str, func=None, rounds: int = 7) -> tuple[float, float]:
    """兩段輸入交錯量測（小、大、小、大…），各取最小值：負載忽高忽低時兩邊受到的影響相同，比「先量完一批小的、再量一批大的」穩。"""
    func = func or lr.redact
    small = big = float("inf")
    for _ in range(rounds):
        start = time.process_time()
        func(small_text)
        small = min(small, time.process_time() - start)
        start = time.process_time()
        func(big_text)
        big = min(big, time.process_time() - start)
    return small, big


_SHAPE_IDS = {"ids": [f"{kind}:{name}" for kind, name in SHAPES]}

# 時間類的測試不吃機器負載（獨立審查 N-3：原本的門檻是 50 ms，最壞形狀在本機 31～42 ms，CI 較慢的節點可能偶發失敗）：
# 絕對上限放寬到實測最壞值（約 30 ms）的 8 倍，只攔「變成秒級」的災難；真正守住複雜度的是「輸入加倍、時間不超過 2.8 倍」
# （線性約 2 倍：128 KiB 比 64 KiB 多一點快取壓力，滿載的機器上 31 個形狀量到 1.7～2.5；平方是 4 倍；小、大兩段交錯量測、各取
# 7 次最小值，量的是比值、與機器快慢無關）與下面的 Python line event 測試。
ABSOLUTE_LIMIT_SECONDS = 0.25
DOUBLING_LIMIT = 2.8


@pytest.mark.parametrize("key", list(SHAPES), **_SHAPE_IDS)
def test_pathological_shape_single_redaction_time_is_bounded(key):
    """單次 redact() 的絕對上限很寬（取 5 次最小值）；本機實測見 PR 說明。"""
    text = _shape(key)
    best = _best_redact(text)
    assert best < ABSOLUTE_LIMIT_SECONDS, f"{key}: {best * 1000:.1f} ms"
    assert lr.redact(lr.redact(text)) == lr.redact(text)


@pytest.mark.parametrize("key", list(SHAPES), **_SHAPE_IDS)
def test_pathological_shape_cost_grows_linearly(key):
    size = SHAPES[key][1]
    small = big = 0.0
    for _ in range(3):  # 機器忽快忽慢（共用的 CI 節點）時重量一次：超線性的成本每次都超過，雜訊不會三次都超過
        small, big = _best_pair(_shape(key, size // 2), _shape(key, size))
        if small <= 0.004 or big / small < DOUBLING_LIMIT:  # 太快時量不準
            return
    pytest.fail(f"{key}: {small * 1000:.2f} ms -> {big * 1000:.2f} ms for 2x input")


@pytest.mark.parametrize("key", list(SHAPES), **_SHAPE_IDS)
def test_pathological_shape_has_no_per_segment_python_work(key):
    size = SHAPES[key][1]
    small = _python_lines(lr.redact, _shape(key, size // 16))
    big = _python_lines(lr.redact, _shape(key, size))
    assert big < 1500, f"{key}: {big} python lines"
    assert big <= small + 400, f"{key}: python lines grow with input: {small} -> {big}"


@pytest.mark.parametrize("key", list(SHAPES), **_SHAPE_IDS)
def test_pathological_shape_access_record_cost_is_bounded(access_sinks, key):
    """完整 uvicorn.access 紀錄（record factory、filter、AccessFormatter、兩個出口），path 先限 4096 字元掃描窗：每筆 20 ms 內。"""
    path = _shape(key, _SIZE)
    best = float("inf")
    for _ in range(3):
        start = time.process_time()
        _log_access(access_sinks.logger, path)
        best = min(best, time.process_time() - start)
    stdout, _ = access_sinks.read()
    assert best < 0.02, f"{key}: {best * 1000:.2f} ms"
    assert all(len(line) < 2300 for line in stdout.splitlines())


@pytest.mark.parametrize("unit", ["/p%2572ivate_x", "/%2570%2572%2569vate_x", "/a%2541", "/%252525252541x", "/x%41%2525"])
def test_truncation_does_not_cut_inside_a_multi_layer_escape(unit):
    """遮好之後超過 128 KiB 要截斷時，切點不可落在多輪編碼的跳脫中間（``%2572`` 切成 ``%25``、``%257``）：第一輪解碼把
    ``%25`` 換成 ``%``，第二次遮罩看到不完整的 ``%`` 就整句「無法判斷」，結果不冪等（443e95a 在這些對齊就會發生；送進來的
    字串本身先截的那一刀照舊，切在中間時第一次就判定無法判斷，兩次結果相同）。"""
    for pad in range(0, 30):
        for head in ("", "?token=abc ", "?token=%20abc "):
            text = (head + "x" * pad + unit * 20_000)[:200_000]
            once = lr.redact(text)
            assert lr.redact(once) == once, (pad, head, once[-40:])


# ---------------------------------------------------------------------------
# 4. 與凍結的 443e95a 模組做差分 fuzz：舊版遮的，新版一定遮；冪等
# ---------------------------------------------------------------------------


def _load_frozen_443e95a():
    path = Path(__file__).parent / "fixtures" / "log_redaction_443e95a.py"
    spec = importlib.util.spec_from_file_location("_frozen_log_redaction_443e95a", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD = _load_frozen_443e95a()


def test_frozen_module_is_443e95a_verbatim():
    path = Path(__file__).parent / "fixtures" / "log_redaction_443e95a.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "ab8cbeebf55b97e4306a6cbda046b5042dea9377da4b28dfd5b0dc97349c253c")


# 兩個秘密由互不重疊、也不在任何鍵名與十六進位數字裡的字母組成；以 6 個字元的窗口追蹤（任何窗口在輸入的其他地方都不出現）。
SECRET_A = "QZJWXYHGLMUWQZYJ"
SECRET_B = "VKPRSITNOVPKRTSN"
_WINDOW = 6


def _windows(secret: str) -> list[str]:
    return [secret[i:i + _WINDOW] for i in range(len(secret) - _WINDOW + 1)]


_WINDOWS = {"A": _windows(SECRET_A), "B": _windows(SECRET_B)}


def _decode_to_fixpoint(text: str) -> str:
    for _ in range(64):
        decoded = unquote(text, errors="replace")
        if decoded == text:
            break
        text = decoded
    return text


def _shown(text: str, windows: list[str]) -> set[str]:
    decoded = _decode_to_fixpoint(text)
    return {w for w in windows if w in text or w in decoded}


def _spell(char: str, r: random.Random) -> str:
    """一個字元在紀錄裡的各種拼法，含十六進位數字本身也編碼、多輪解碼才還原的寫法。"""
    if r.random() < 0.3:
        return char
    if ord(char) > 127:
        return quote(char, safe="")
    hh = f"{ord(char):02X}"
    d1, d2 = (f"%{ord(digit):02X}" for digit in hh)
    form = r.choice(["%" + hh, "%25" + hh, "%2525" + hh, "%" + d1 + d2, "%" + d1 + hh[1], "%" + hh[0] + d2,
                     "%25" + d1 + d2, "%2%35" + hh])
    return form.lower() if r.random() < 0.3 else form


_SEPARATORS = [quote(c, safe="") for c in ' \t"\'?#/\\%&;=+*<>\xa0　 “'] + [
    "'", '"', ";", "=", "*", "+", "\\", "<", "%2520", "%253F", "%2526", "%", "%2", "%zz", "%%33%46", "%3%46",
    "%%32%36", "%C0%AF", "***", "�"]
_NAMES = [("token",), ("access", "token"), *CREDENTIAL_WORDS]
_OTHER_PARAMS = ["limit", "page", "x", "session_id", "mytoken", "token_type", "keys", "key_id", "monkey", "to.ken", "to ken",
                 "signature_version", "auth_mode", "tokens", "mode", "q"]
_BASES = ["/api/logs/stream", "/x", "/private_x/mcp", "/private_ab12/messages/", "/p%5Crivate_x/mcp", "/%2570rivate_x",
          "", "//private_y/sse", "http://h/x"]
_PREFIXES = ["/private_", "//private_", "/p%72ivate_", "%2Fprivate_", "/%2570rivate_", "\\private_", "/p%5Crivate_",
             "/PRIVATE_", "/p%%37%32ivate_"]


def _name(r: random.Random) -> str:
    words = r.choice(_NAMES)
    name = r.choice(["_", "-", "", "%5F"]).join(words)
    if r.random() < 0.3:
        name = name.upper()
    if r.random() < 0.15:
        name = "".join(_spell(c, r) if c.isalpha() else c for c in name)
    return name


def _value(r: random.Random, secret: str) -> str:
    roll = r.random()
    if roll < 0.3:
        return secret
    if roll < 0.55:
        return r.choice(_SEPARATORS) + secret
    if roll < 0.7:
        return secret + r.choice(_SEPARATORS)
    if roll < 0.85:
        return secret[:8] + r.choice(_SEPARATORS) + secret[8:]
    return "".join(_spell(c, r) if r.random() < 0.15 else c for c in secret)


def _query_text(r: random.Random) -> tuple[str, list[str]]:
    secrets: list[str] = []
    parts = [r.choice(["", "", "GET ", "msg: ", '"']), r.choice(_BASES)]
    for index in range(r.randint(1, 4)):
        lead = ("?" if index == 0 else r.choice("&&;?")) if r.random() < 0.85 else _spell(r.choice("?&;"), r)
        roll = r.random()
        if roll < 0.55 and len(secrets) < 2:
            key = "B" if "B" not in secrets else "A"
            parts.append(lead + _name(r) + (_spell("=", r) if r.random() < 0.15 else "=")
                         + _value(r, SECRET_B if key == "B" else SECRET_A))
            secrets.append(key)
        elif roll < 0.75:
            parts.append(lead + r.choice(_OTHER_PARAMS) + "=" + r.choice(["1", "abc", "%20", "x%26y", "", "a/b"]))
        elif roll < 0.85 and "A" not in secrets:
            parts.append(lead + _value(r, SECRET_A))  # 沒有 = 的裸參數
            secrets.append("A")
        else:
            parts.append(lead + r.choice(["", "&", "=", "%26", "#f", "/private_z", "%"]))
    parts.append(r.choice(["", "", " HTTP/1.1", " done", "#frag", "' x", "/mcp", "&", ";"]))
    return "".join(parts), secrets


def _segment_text(r: random.Random) -> tuple[str, list[str]]:
    secrets = []
    parts = [r.choice(["", "GET ", "x /a", "?", "/api?q=1&", "/x?u="])]
    key_first = r.random() < 0.2
    keypart = None
    if r.random() < 0.75:
        keypart = _spell(r.choice("?&;"), r) + _name(r) + _spell("=", r) + _value(r, SECRET_B)
    if key_first and keypart:
        parts.append(keypart + r.choice(["", " ", "/", "&"]))
        secrets.append("B")
    parts.append(r.choice(_PREFIXES))
    for _ in range(r.randint(0, 2)):
        parts.append(r.choice(_SEPARATORS))
    if r.random() < 0.85:
        parts.append(_value(r, SECRET_A))
        secrets.append("A")
    parts.append(r.choice(["", "/mcp", "/x", "/messages/", "?x=1", " HTTP/1.1"]))
    if keypart and not key_first:
        parts.append(keypart)
        secrets.append("B")
    parts.append(r.choice(["", "", "?a=b&c", "&x=1", " done", "/sse"]))
    return "".join(parts), secrets


def _access_line(module, path: str) -> str:
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5555", "GET", path, "1.1", 401), None)
    module.scrub_record(record)
    return record.getMessage()


def test_differential_fuzz_never_shows_what_443e95a_masked():
    """redact() 與 access log 兩層：443e95a 遮掉的秘密窗口，新版一定也遮掉；新版的結果再遮一次不變。"""
    r = random.Random(20261114)
    checked = access_checked = 0
    for index in range(16_000):
        text, secrets = (_query_text if index % 2 else _segment_text)(r)
        windows = [w for key in secrets for w in _WINDOWS[key]]
        present = _shown(text, windows)
        if not present:
            continue
        checked += 1
        old, new = OLD.redact(text), lr.redact(text)
        lost = (_shown(new, windows) & present) - _shown(old, windows)
        assert not lost, (text, old, new)
        assert lr.redact(new) == new, (text, new)
        printable = text.isascii() and text.isprintable() and " " not in text
        path = _uvicorn_access_path(text) if printable and text.startswith("/") else None
        if path is None:
            continue
        access_checked += 1
        old_line, new_line = _access_line(OLD, path), _access_line(lr, path)
        lost = (_shown(new_line, windows) & _shown(path, windows)) - _shown(old_line, windows)
        assert not lost, (path, old_line, new_line)
    assert checked > 12_000 and access_checked > 2_000, (checked, access_checked)
