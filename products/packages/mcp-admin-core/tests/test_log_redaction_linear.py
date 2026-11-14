"""log_redaction 的複雜度、長度上限、uvicorn.access 形狀與反斜線寫法的回歸測試。

背景（f-review-regressions）：redact() 對 ``'/'*N + '?x'`` 之類的輸入是 O(n^2)，而 uvicorn.access 在事件迴圈
執行緒同步呼叫 scrub_record，未認證的單一請求就能讓整個服務卡數十秒；整句替換時 args 變成空 tuple 也讓
AccessFormatter 拆參數失敗。所有憑證都是合成值。
"""
from __future__ import annotations

import io
import logging
import re
import time

import pytest

from mcp_admin_core import log_redaction as lr

# 字元集刻意避開佔位字串與 truncate 標記會用到的字母，方便斷言「憑證任何字元都沒出現」。
SECRET = "QZJWXKQZJWXK"

N = 65536


def _shapes(n: int) -> dict[str, str]:
    return {
        "slash+?x": "/" * n + "?x",
        "slash+%": "/" * n + "%",
        "slash+%zz": "/" * n + "%zz",
        "%": "%" * n,
        "?": "?" * n,
        "&": "&" * n,
        ";": ";" * n,
        "?&": "?" + "&" * n,
        "?;": "?" + ";" * n,
        "?a=a=": "?" + "a=" * (n // 2),
        "&=&=": "&=" * (n // 2),
        "?&;mix": "?" + "&a;" * (n // 3),
        "?&space": "?" + "& " * (n // 2),
        "?fffd&": "?" + "\ufffd&" * (n // 2),
        "?%26": "?" + "%26" * (n // 3),
        "?k127&": ("?" + "k" * 126 + "&") * (n // 128),
        "mixed-slash": "/a//b///" * (n // 8) + "?x",
        "?token&": "?" + "token&" * (n // 6),
        "?t&o&k&e&n": "?" + "t&o&k&e&n&" * (n // 10),
        "a=": "a=" * (n // 2),
        "&a=": "&a=" * (n // 3),
        "?a=&": "?a=&" * (n // 4),
        "/private_": "/private_" * (n // 9),
        "/private_x": "/private_x" * (n // 10),
        "http://": "http://" * (n // 7),
        "http://a/%": "http://a/%" * (n // 10),
        "/a%": "/a%" * (n // 3),
        "%25*N": "%25" * (n // 3),
        "/%25*N": "/" + "%25" * (n // 3),
        "layered": ("/%" + "25" * 14 + "41") * (n // 30),
        "layered-private": ("/%" + "25" * 6 + "70rivate_x") * (n // 20),
        "%2f": "%2f" * (n // 3),
        "%2fprivate_": "%2fprivate_" * (n // 11),
        "space/%": " /%" * (n // 3),
        "token=": "?token=" * (n // 7),
        "&token=x": "&token=x" * (n // 8),
        "backslash": "\\" * n,
        "backslash/": "\\/" * (n // 2),
        "\\private_": "\\private_" * (n // 9),
        "/private+backslash": "/private" + "\\" * n,
        "/p\\\\+x": "/p" + "\\" * n + "x",
        "ctrl%0a": "%0a" * (n // 3),
        "long-token": "/private_" + "x" * n,
        "long-q-token": "?token=" + "x" * n,
        "long-key": "?" + "k" * n + "=v",
        "ab/?": "ab/" * (n // 3) + "?",
        "unicode": "é/%" * (n // 3),
        "fffd": "/\ufffd" * (n // 2) + "?x",
    }


def _best_of(text: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        lr.redact(text)
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("name", list(_shapes(1)))
def test_redact_is_fast_on_pathological_inputs(name):
    """64KB 病態輸入在 50ms 內（取 3 次最小值，抗 CI 雜訊），且 4 倍長度的耗時不超過線性的 ~9 倍。

    O(n^2) 的版本 64KB 要數秒，4 倍長度是 16 倍；兩個條件都能抓到退回二次複雜度。
    """
    big = _best_of(_shapes(N)[name])
    assert big < 0.05, f"{name}: {big * 1000:.1f}ms for 64KB"
    small = _best_of(_shapes(N // 4)[name])
    # 小輸入太快時量不準：只在有足夠耗時時才比對成長比例。
    if small > 0.002:
        assert big / small < 9, f"{name}: {small * 1000:.2f}ms -> {big * 1000:.2f}ms for 4x input"


def test_redact_is_bounded_for_huge_input():
    """遠超上限的輸入也只處理上限內的部分（耗時有明確上界）。"""
    huge = "/" * (lr.MAX_REDACT_CHARS * 20) + "?x"
    start = time.perf_counter()
    out = lr.redact(huge)
    assert time.perf_counter() - start < 0.5
    assert len(out) < lr.MAX_REDACT_CHARS + 100


# ---------------------------------------------------------------------------
# 長度上限：不可洩漏 token 的任何部分；一般長 traceback 不可整段消失
# ---------------------------------------------------------------------------


def test_truncation_keeps_head_of_ordinary_long_text():
    lines = "".join(f"  File \"/app/mod{i}.py\", line {i}, in handler\n" for i in range(8000))
    assert len(lines) > lr.MAX_REDACT_CHARS
    out = lr.redact(lines)
    assert out.startswith(lines[:2000])
    assert "…[truncated " in out
    assert lr.AMBIGUOUS not in out
    # 截在換行邊界，不會留下半行
    assert out.split(" …[truncated ")[0].endswith("in handler")


@pytest.mark.parametrize("offset", [-40, -25, -18, -12, -10, -9, -8, -5, -3, -1, 0, 1, 5, 12, 30])
def test_truncation_never_leaks_part_of_a_token(offset):
    """標記與 token 橫跨上限（含切在標記中間、切在 token 中間）時，token 的任何字元都不能出現。"""
    for prefix in ("/private_", "?token=", "/p%72ivate_", "%2Fprivate_", "/private\\_", "&access_token="):
        # 單一超長 URL（沒有空白可以退到邊界）：標記起點落在 上限+offset
        start = lr.MAX_REDACT_CHARS - len(prefix) + offset
        text = "a" * start + prefix + SECRET
        out = lr.redact(text)
        assert not (set(SECRET) & set(out.replace("truncated", "").replace("chars", ""))), (prefix, offset, out[-80:])


def test_truncation_prefers_whitespace_boundary():
    filler = ("x" * 99 + " ") * (lr.MAX_REDACT_CHARS // 100 + 50)
    text = filler + "/private_" + SECRET
    out = lr.redact(text)
    assert SECRET not in out and "…[truncated " in out
    assert not out.split("…[truncated ")[0].endswith("%")


def test_truncated_dangling_escape_does_not_fail_closed():
    base = "/" + "a" * (lr.MAX_REDACT_CHARS - 2)  # 上限處恰好切在 '%' 之後
    for tail in ("%", "%2"):
        out = lr.redact(base[: lr.MAX_REDACT_CHARS - len(tail)] + tail + "b" * 50)
        assert lr.AMBIGUOUS not in out


# ---------------------------------------------------------------------------
# 遮罩涵蓋率（f-4）：反斜線、過長 UTF-8
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "\\private_{s}",
    "/private\\_{s}",
    "/\\private_{s}/mcp",
    "/p\\rivate_{s}",
    "\\\\private_{s}",
    "GET \\private_{s} HTTP/1.1",
    "/%5cprivate_{s}",
    "%5Cprivate_{s}",
    "/private%5c_{s}",
    "/%e0%80%afprivate_{s}",
    "/private_{s}\\x",
    "/api?to\\ken={s}",
])
def test_backslash_and_overlong_variants_are_masked(raw):
    out = lr.redact(raw.format(s=SECRET))
    assert SECRET not in out, out


def test_backslashes_in_ordinary_text_are_untouched():
    for text in ("C:\\Users\\x\\file.py", "line1\\nline2", "regex \\d+ here", "/api/x\\y"):
        assert lr.redact(text) == text


# ---------------------------------------------------------------------------
# uvicorn.access：整句要替換時保留 5 元組（f-3）
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.errors: list[BaseException | None] = []
        self.lines: list[str] = []

    def handleError(self, record):  # noqa: N802
        import sys

        self.errors.append(sys.exc_info()[1])

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def access_logger():
    from uvicorn.logging import AccessFormatter

    lr.install()
    handler = _Capture()
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    logger = logging.getLogger("uvicorn.access")
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield logger, handler
    logger.removeHandler(handler)
    logger.setLevel(old_level)
    logger.propagate = old_propagate


@pytest.mark.parametrize("path", [
    "/healthz?a=%zz",
    "/healthz?a=%",
    "/private_" + SECRET + "?a=%zz",
    "/private_" + SECRET + "%zz",
    "/x?token=" + SECRET + "&y=%zz",
    "/%2570rivate_" + SECRET + "/mcp?%zz",
    "/" + "%25" * 40 + "74oken=" + SECRET,
    "/x?a=%0d%0a" + SECRET,
    "\\private_" + SECRET + "%zz",
    "/private\\_" + SECRET,
])
def test_uvicorn_access_line_survives_ambiguous_paths(access_logger, path):
    logger, handler = access_logger
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", path, "1.1", 404)
    assert handler.errors == []
    assert len(handler.lines) == 1
    line = handler.lines[0]
    assert line.startswith('127.0.0.1:5555 - "GET ') and line.endswith('HTTP/1.1" 404 Not Found'), line
    assert not (set(SECRET) & set(line)), line


def test_uvicorn_access_ambiguous_path_keeps_five_tuple_shape():
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1", "GET", "/healthz?a=%zz", "1.1", 200), None)
    lr.scrub_record(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5
    assert record.args[0] == "10.0.0.1:1" and record.args[1] == "GET" and record.args[3:] == ("1.1", 200)
    assert record.args[2] == lr.AMBIGUOUS
    assert record.msg == '%s - "%s %s HTTP/%s" %d'


def test_uvicorn_access_suspicious_other_args_are_replaced_with_shape_kept():
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1", "GET /api?token%", "3dSECRET", "1.1", 200), None)
    lr.scrub_record(record)
    assert len(record.args) == 5 and record.args[4] == 200
    assert "SECRET" not in record.getMessage()


def test_uvicorn_access_normal_path_is_unchanged():
    args = ("10.0.0.1:1", "GET", "/api/profiles?limit=5", "1.1", 200)
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    lr.scrub_record(record)
    assert record.args == args


# ---------------------------------------------------------------------------
# 其他 logger 的整句替換不能讓 logging 爆掉
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("template", "args"), [
    ("GET %s%s", ("/api/logs?token=", SECRET)),
    ("req %s", ("/healthz?a=%zz",)),
    ("req %s", ("/private_" + SECRET + "%zz",)),
    ("req %(u)s", ({"u": "/private_" + SECRET + "%zz"},)),
    ("GET /api/%s?token=" + SECRET, ("x",)),
    ("bad %d", ("not-an-int",)),
    ("no args /private_" + SECRET + "%zz", ()),
    ("%s", (["/private_" + SECRET + "%zz"],)),
])
def test_other_loggers_whole_line_replacement_does_not_break_logging(template, args):
    lr.install()
    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    logger = logging.getLogger("some.other.logger")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info(template, *args)
    finally:
        logger.removeHandler(handler)
    assert handler.errors == []
    assert len(handler.lines) == 1
    assert not (set(SECRET) & set(handler.lines[0])), handler.lines[0]


def test_exception_traceback_with_long_path_is_fast_and_masked():
    lr.install()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        try:
            raise RuntimeError("failed " + "/" * N + "?x /private_" + SECRET)
        except RuntimeError:
            start = time.perf_counter()
            logger.exception("boom")
            assert time.perf_counter() - start < 0.2
    finally:
        logger.removeHandler(handler)
    # traceback 的 `File "<路徑>", line N, in <函式>` 是執行環境決定的（CI 工作目錄 /workspace/WOOWTECH/… 含 W、K
    # 等大寫字母），與遮罩無關；先去掉這些行，其餘（例外訊息、原始碼行）仍逐字元比對秘密的任何片段。
    output = re.sub(r'(?m)^[ \t]*File ".*", line \d+.*$', "", stream.getvalue())
    assert "RuntimeError" in output and "failed" in output  # 確實比對到例外訊息而不是空字串
    assert not (set(SECRET) & set(output))


# ---------------------------------------------------------------------------
# 完整路徑效能（r2co-1）：真的 uvicorn.access logger、已安裝的 record factory 與 filter、真的 AccessFormatter。
# 只量 redact() 看不出 factory + filter 重複遮與 formatter 的成本。
# ---------------------------------------------------------------------------


def _log_access(logger: logging.Logger, path: str) -> None:
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", path, "1.1", 200)


def _best_full(logger, handler, path: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        handler.lines.clear()
        start = time.perf_counter()
        _log_access(logger, path)
        best = min(best, time.perf_counter() - start)
    assert len(handler.lines) == 1 and handler.errors == []
    return best


@pytest.mark.parametrize("name", list(_shapes(1)))
def test_full_access_path_is_fast_and_linear(access_logger, name):
    """64KB 病態路徑走完整 uvicorn.access 路徑（factory、filter、AccessFormatter）在 50ms 內，4 倍長度近線性。"""
    logger, handler = access_logger
    big = _best_full(logger, handler, _shapes(N)[name])
    assert big < 0.05, f"{name}: {big * 1000:.1f}ms for 64KB (full path)"
    small = _best_full(logger, handler, _shapes(N // 4)[name])
    if small > 0.002:
        assert big / small < 9, f"{name}: {small * 1000:.2f}ms -> {big * 1000:.2f}ms for 4x input"
    # access log 行長度有上界（G3／G4）。
    assert len(handler.lines[0]) < 2300


# ---------------------------------------------------------------------------
# uvicorn.access 的 path 截斷（G3／G4）
# ---------------------------------------------------------------------------


def _access_record(path):
    return logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1", "GET", path, "1.1", 200), None)


def test_access_path_over_2kib_is_truncated_and_shape_kept():
    path = "/" + "a" * 40000
    record = _access_record(path)
    lr.scrub_record(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5
    client, method, shown, version, status = record.args
    assert (client, method, version, status) == ("10.0.0.1:1", "GET", "1.1", 200)
    cut = len(path) - 40001 + lr.ACCESS_PATH_MAX_CHARS
    assert shown == path[:lr.ACCESS_PATH_MAX_CHARS] + f" …[truncated {len(path) - lr.ACCESS_PATH_MAX_CHARS} chars]"
    assert cut == lr.ACCESS_PATH_MAX_CHARS
    assert len(record.getMessage()) < 2200


@pytest.mark.parametrize("length", [1, 100, 2047, 2048])
def test_access_path_up_to_limit_is_unchanged(length):
    path = "/" + "a" * (length - 1)
    record = _access_record(path)
    lr.scrub_record(record)
    assert record.args[2] == path


@pytest.mark.parametrize("offset", range(-12, 13))
def test_access_path_truncation_never_leaks_token(offset):
    for prefix in ("/private_", "?token=", "&access_token=", "/p%72ivate_", "%2Fprivate_", "/private\\_", ";to\ufffdken="):
        start = lr.ACCESS_PATH_MAX_CHARS - len(prefix) + offset
        path = "/" + "a" * start + prefix + SECRET
        record = _access_record(path)
        lr.scrub_record(record)
        line = record.getMessage()
        assert not (set(SECRET) & set(line.replace("truncated", "").replace("chars", ""))), (prefix, offset, line[-90:])


@pytest.mark.parametrize("tail", ["%41", "%2f", "%e4%b8%ad"])
def test_access_path_truncation_does_not_leave_dangling_escape(tail):
    for pad in (lr.ACCESS_PATH_MAX_CHARS - 1, lr.ACCESS_PATH_MAX_CHARS - 2, lr.ACCESS_PATH_MAX_CHARS - 3):
        record = _access_record("/" + "a" * (pad - 1) + tail + "b" * 200)
        lr.scrub_record(record)
        assert record.args[2] != lr.AMBIGUOUS
        assert "…[truncated " in record.args[2]
        assert not record.args[2].split("…[truncated ")[0].endswith("%")


def test_access_path_token_at_head_is_masked_before_truncation():
    record = _access_record("/private_" + SECRET + "/" + "a" * 5000)
    lr.scrub_record(record)
    assert record.args[2].startswith("/private_***/")
    assert not (set(SECRET) & set(record.getMessage().replace("truncated", "").replace("chars", "")))


# ---------------------------------------------------------------------------
# 鍵名含（解碼後的）分隔符、空白的寫法仍要遮（r2co-1 不可用「key 遇到分隔符就停」的寫法回歸）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "/x?token%26={s}",
    "/x?token%3B={s}",
    "/x?token%20={s}",
    "/x?to%26ken={s}",
    "/x?to%3Bken={s}",
    "/x?to%20ken={s}",
    "/x?access%26_%3Btoken%20={s}",
    "/x?a=1&to&ken={s}",
    "/x?a=1;to;ken={s}",
    "/x?to&ken={s}&y=1",
    "/x?&&&token&&&={s}",
    "/x?to\\ken={s}",
    "/x?to\ufffdken={s}",
    "/x?\ufffdaccess\ufffd_token={s}",
])
def test_key_with_separator_or_noise_between_letters_is_masked(raw):
    out = lr.redact(raw.format(s=SECRET))
    assert SECRET not in out, out


@pytest.mark.parametrize("text", ["/x?xtoken=abc", "/x?tokens=abc", "/x?a=1&b=token", "/x?t=1&o=2&k=3&e=4&n=5", "/x?x.token=a"])
def test_key_rule_does_not_overmask_other_params(text):
    assert lr.redact(text) == text


# ---------------------------------------------------------------------------
# r2-sec-1：U+FFFD 與反斜線在前置判斷與後續比對的處理一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "GET /p\ufffdrivate_{s}/mcp",
    "GET /p\\rivate_{s}/mcp",
    "GET /\ufffd\ufffdprivate_{s}/mcp",
    "x ;to\ufffdken={s}",
    "x ;to\\ken={s}",
    "x &t\ufffdo\\ken={s}",
    "x &access_\ufffdtoken={s}",
])
def test_fffd_and_backslash_inside_marker_are_masked_without_other_trigger(raw):
    text = raw.format(s=SECRET)
    assert "?" not in text and "%" not in text
    out = lr.redact(text)
    assert SECRET not in out, out
    assert lr._has_candidate(text)


# ---------------------------------------------------------------------------
# F7 第二輪：access 前綴字樣（scope=access 之類的值）後接 token 的各種分隔符寫法不可外洩
# （驗證者 P2：舊的 lead 允許為空時 match 吃掉分隔符卻不遮）
# ---------------------------------------------------------------------------

_SEPS = ["%20", "%26", "%3B", "%3b", "[]", " ", ".", "_", "%5b%5d", "\\", "�", "&", ";"]


@pytest.mark.parametrize("prefix", [
    "/x?scope=access&", "/x?scope=access;", "/x?a=access&", "/x?foo=access;", "/x?a=access&&", "/x?a=access%26",
    "/x?a=access%3B", "/x?a=access&access&", "/x?access&", "/x?access=access&", "/x?a=1&access;",
])
@pytest.mark.parametrize("key", ["token%20", "token%26", "token%3B", "to%20ken", "token[]", "token ", "to.ken", "token_", "to&ken",
                                 "access_token", "token"])
def test_access_word_before_token_key_is_still_masked(prefix, key):
    out = lr.redact(f"{prefix}{key}={SECRET}")
    assert SECRET not in out, out


@pytest.mark.parametrize("sep", _SEPS)
def test_access_word_then_separator_variants_are_masked(sep):
    for text in (f"/x?scope=access&token{sep}={SECRET}", f"/x?scope=access;token{sep}={SECRET}",
                 f"/x?a=access&to{sep}ken={SECRET}"):
        assert SECRET not in lr.redact(text), text


def test_access_word_then_token_through_real_access_line(access_logger):
    logger, handler = access_logger
    for path in ("/x?scope=access&token%20=" + SECRET, "/x?scope=access&token%26=" + SECRET,
                 "/x?a=access&token[]=" + SECRET):
        logger.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", path, "1.1", 200)
    assert handler.lines and all(SECRET not in line for line in handler.lines), handler.lines



@pytest.mark.parametrize("text", [
    "=&access_token%3Da.-%26token_={s}",       # 前一個值吞進下一個 lead 的前半（差分 fuzz 找到的唯一一筆）
    "?token=.&token=.&token=.&token_={s}",
    "?a=-&token%20={s}",
    "?access_token=x.;to.ken={s}",
])
def test_value_swallowing_part_of_next_lead_does_not_hide_next_key(text):
    out = lr.redact(text.format(s=SECRET))
    assert SECRET not in out, out
