"""F10（第三輪）：log 遮罩「每段一次 Python 工作」、access path 掃描窗、截斷冪等、單次遮罩標記的回歸測試。

背景：G5 在真 Odoo 沙盒 pod 內，未認證的 ``/x?`` 接 3 萬個 ``&=``（60KB）20 並行，/healthz 最大 1.3 秒。成本不只一處：
``_mask_loose_keys`` 對每個 lead 連續段、``_has_invalid_escape_in_url`` 對每個 run、``urllib.parse.unquote`` 對每個 ``%``
各跑一次 Python 層工作，段數由攻擊者決定。所有憑證都是合成值。
"""
from __future__ import annotations

import logging
import random
import re
import sys
import time
from urllib.parse import unquote

import pytest

from mcp_admin_core import log_redaction as lr

# 字元集刻意避開標記與佔位字串會用到的字母，方便斷言「憑證任何字元都沒出現」。
SECRET = "QZJWXKQZJWXK"
SECRET_CHARS = set(SECRET)

N = 64_000


def _leaked(text: str) -> bool:
    return bool(SECRET_CHARS & set(text))


# ---------------------------------------------------------------------------
# 參考實作：「每段一次」的 Python 迴圈版（lead 連續段、單點 _KEY_TAIL.match），單一 sub 的結構必須與它逐字相同。
# 鍵名本身（_KEY_TAIL）的涵蓋範圍另由下面的「ff3dbc0／ec82641 超集」測試守住（F10b：不再與 ff3dbc0 逐字相同）。
# ---------------------------------------------------------------------------

_REF_LEAD_RUN = re.compile(r"[^A-Za-z0-9=#\"']+")
_REF_RUN = re.compile(r"[^\s\"']+")
_REF_INVALID = re.compile(r"%(?![0-9a-fA-F]{2})")


def _ref_mask_loose_keys(text: str) -> str:
    out: list[str] = []
    last = 0
    for run in _REF_LEAD_RUN.finditer(text):
        if run.end() <= last:
            continue
        lead = text[max(run.start(), last):run.end()]
        if "?" not in lead and "&" not in lead and ";" not in lead:
            continue
        match = lr._KEY_TAIL.match(text, run.end())
        if match is None:
            continue
        out.append(text[last:match.start()])
        out.append(match.group(1) + "=" + lr.MASK)
        last = match.end()
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _ref_has_invalid_escape_in_url(text: str) -> bool:
    for run in _REF_RUN.finditer(text):
        start = text.find("/", run.start(), run.end())
        if start != -1 and _REF_INVALID.search(text, start + 1, run.end()):
            return True
    return False


_KEYS = ["token", "access_token", "accesstoken", "access", "Token", "TOKEN", "tok", "t", "o", "k", "e", "n", "a", "s",
         "ſ", "K", "tOKen", "x", "xtoken"]
_LEAD = ["?", "&", ";", "=", "#", " ", "\t", "\n", "　", "\u0085", "\\", "�", "'", '"', "-", ".", "_", "/", ":",
         "é", "​", "\x00", "!", "*", "{"]
_ENC = ["%26", "%3b", "%3B", "%3f", "%20", "%3d", "%25", "%2526", "%5c", "%2f", "%00", "%e0%80%af", "%0a", "%27", "%ef%bf%bd",
        "%c3%a9", "%e2%84%aa", "%", "%2", "%zz", "%g1", "%%", "%41", "%72"]
_VAL = ["S", "SECRET", "A.-", "S&x=1", "S;z", "S#f", "a b", "a'b", "S�", "\\S", "Ü", "1"]
_PRIV = ["/private_", "%2fprivate_", "/p\\rivate_", "/", "//", "http://", "a/b", "/x?"]
_UQ = ["%", "%4", "%41", "%c3", "%a9", "%e2", "%80", "%ff", "%00", "%5c", "\\", "a", "é", "…", "�", "\ud800",
       "\udc80", "%25", "%zz", "%C3", "\x00", "\n", "/", "%F0", "%9F", "%98", "%ed", "%a0", "%e0", "%c0", "%af", "\x7f",
       "\x80", "\\x41", "\\\\", "%5", "%e2%84%aa", "%f0%9f%98%80", "\U0001F600", "́"]


def _gen(r: random.Random) -> str:
    if r.random() < 0.3:
        return "".join(r.choice(_UQ) for _ in range(r.randint(1, 14)))
    parts = []
    for _ in range(r.randint(1, 12)):
        k = r.random()
        if k < 0.25:
            parts.append(r.choice(_KEYS))
        elif k < 0.5:
            parts.append(r.choice(_LEAD))
        elif k < 0.62:
            parts.append(r.choice(_ENC))
        elif k < 0.72:
            parts.append("=")
        elif k < 0.82:
            parts.append(r.choice(_VAL))
        elif k < 0.92:
            parts.append(r.choice(_PRIV))
        else:
            parts.append(r.choice(_LEAD) * r.randint(2, 40))
    text = "".join(parts)
    if r.random() < 0.5:
        text = "/p?" + text
    if r.random() < 0.3:
        text += r.choice(["=S", "=SECRET", "&token=S"])
    return text


def test_loose_key_masking_matches_per_segment_reference():
    r = random.Random(20261112)
    for _ in range(20_000):
        text = _gen(r)
        assert lr._mask_loose_keys(text) == _ref_mask_loose_keys(text), repr(text)


@pytest.mark.parametrize("text", [
    "token=a.-&token_=S",
    "?token=a.-&token_=S",
    "/p?scope=access&token%20=S",
    "?Koken=S",  # Kelvin 不是 ASCII t／k：不是鍵
    "?toKen=S",
    "?acceſſtoken=S",
    "?ſ&token=S",
    "&&&&token=S",
    "?�&\\token=S",
    "?  token=S",
    "?　token=S",
    "?'token=S",
    "?xtoken=S",
    "?" + "&" * 5000 + "token=S",
    "?" + "&=" * 5000 + "token=S",
])
def test_loose_key_masking_special_cases_match_reference(text):
    assert lr._mask_loose_keys(text) == _ref_mask_loose_keys(text)


# ---------------------------------------------------------------------------
# F10b：鍵名字母之間的雜字元不受 IGNORECASE 影響。遮罩範圍必須是 ff3dbc0 的超集，且不少於 ec82641
# ---------------------------------------------------------------------------

# ff3dbc0 的鬆散鍵名 pattern（凍結副本）：_JUNK 在 IGNORECASE 下把 U+017F／U+212A／U+0130／U+0131 當成字母排除。
_FF_JUNK = r"[^A-Za-z0-9=?#\"']"


def _ff_loose_word(word: str) -> str:
    return "".join(f"{char}{_FF_JUNK}*" for char in word)


_FF_TAIL = (r"((?:" + _ff_loose_word("access") + r")?" + _ff_loose_word("token") + r")=([^&#;\s\"']+)")
_FF_LOOSE_RE = re.compile(r"(?=[?&;][^A-Za-z0-9=#\"'?&;]*+[aAtT])"
                          r"([?&;][^A-Za-z0-9=#\"'?&;]*+)(?i:" + _FF_TAIL + r")")
# ec82641 的鍵名比對（凍結副本）：key 最長 128 字元、小寫後只留 ASCII 英數，剛好是 token／accesstoken。
_OLD_KV_RE = re.compile(r"([?&;])([^=?#\"']{1,128})=([^&#;\s\"']+)")

# 會在 IGNORECASE 下折疊成 ASCII 字母的字元；它們可以夾在鍵名字母之間（雜字元），也可以當 s／k／i。
FOLDERS = ["ſ", "K", "İ", "ı"]


def _positions(spans) -> set[int]:
    out: set[int] = set()
    for start, end in spans:
        out.update(range(start, end))
    return out


def _ff_spans(text: str):
    return [m.span(3) for m in _FF_LOOSE_RE.finditer(text)]


def _old_spans(text: str):
    for m in _OLD_KV_RE.finditer(text):
        key = m.group(2)
        canonical = "".join(c for c in key.lower() if c.isascii() and c.isalnum())
        if canonical in ("token", "accesstoken"):
            yield m.span(3)


def _new_spans(text: str):
    """新版鬆散鍵名 pattern 與 query pattern 一起遮到的值（redact() 兩者都會跑）。"""
    spans = [m.span(3) for m in lr._LOOSE_KEY_RE.finditer(text)]
    spans += [(m.end(1), m.end()) for m in lr._QUERY_TOKEN_RE.finditer(text)]
    return spans


def _check_superset(text: str) -> None:
    covered = _positions(_new_spans(text))
    for name, spans in (("ff3dbc0", _ff_spans(text)), ("ec82641", _old_spans(text))):
        missing = _positions(spans) - covered
        assert not missing, f"{name} 遮、新版不遮：{text!r} 缺 {sorted(missing)[:5]}"


def _fold_key(r: random.Random) -> str:
    """產生 token／access_token，字母之間夾 0～3 個雜字元（含折疊字元），s／k 偶爾直接換成 ſ／K。"""
    word = r.choice(["token", "accesstoken", "access_token"])
    junk = FOLDERS + ["\\", "�", ".", " ", "-", "_", "%", "&", ";", "\t", "　", "é"]
    out = []
    for char in word:
        if char == "s" and r.random() < 0.3:
            char = "ſ"
        elif char == "k" and r.random() < 0.3:
            char = "K"
        elif r.random() < 0.3:
            char = char.upper()
        out.append(char)
        if r.random() < 0.6:
            out.append("".join(r.choice(junk) for _ in range(r.randint(1, 3))))
    return "".join(out)


def test_loose_key_masking_is_superset_of_ff3dbc0_and_ec82641():
    r = random.Random(20261201)
    for _ in range(30_000):
        lead = r.choice(["?", "&", ";", "?\\", "&�", "?  ", "?&", "/x?", "?-.", "?ſ", "?K"])
        tail = r.choice(["", "&y=1", "#f", " z", "'", ";w=2"])
        text = lead + _fold_key(r) + "=" + r.choice(["SECRET", "S", "A.-", "S�"]) + tail
        if r.random() < 0.3:
            text = "?a=1" + text
        _check_superset(text)


@pytest.mark.parametrize("fold", FOLDERS)
@pytest.mark.parametrize("word,gaps", [
    ("token", range(1, 6)),
    ("access_token", range(1, 13)),
])
def test_folding_chars_between_key_letters_do_not_defeat_masking(word, gaps, fold):
    # 字母之間每個位置各夾一次、各夾三次（lead 之後、= 之前也算）
    for gap in gaps:
        for count in (1, 3):
            key = word[:gap] + fold * count + word[gap:]
            for lead in ("?", "&", ";", "/x?"):
                text = f"{lead}{key}=SECRET&y=1"
                # redact() 的必要條件檢查要求整句有 ``?``（ec82641、ff3dbc0 也是）；只用 ``&``／``;`` 開頭時前面補一個
                out = lr.redact(text if "?" in text else "/p?a=1" + text)
                assert "SECRET" not in out, (text, out)
                assert lr._mask_loose_keys(text) == f"{lead}{key}={lr.MASK}&y=1", text
                _check_superset(text)


@pytest.mark.parametrize("text", [
    "?toſken=SECRET",         # ſ 夾在 o 與 k 之間
    "?tKoken=SECRET",         # K 夾在 t 與 o 之間
    "?tokİen=SECRET",         # İ
    "?tokeın=SECRET",         # ı
    "?tokKKen=SECRET",   # k 之後再一個 K 當雜字元
    "?toKKken=SECRET",   # k 之前的 K（k 仍是 ASCII 的 k）
    "?toKen=SECRET",          # K 本身當 k（ff3dbc0 就會遮）
    "?acceſſtoken=SECRET",   # 兩個 ſ 當 access 的兩個 s
    "?acceſſſtoken=SECRET",   # 第三個 ſ 是雜字元
    "?acceſsſtoken=SECRET",   # ſ s ſ：第二個 ſ 是雜字元
    "?acceſsſ_token=SECRET",
    "?accesſſſtoken=SECRET",
    "?accessſKİıtoken=SECRET",
    "?access_tſKİıoken=SECRET",
    "?access_tokſKİıen=SECRET",
    "&access_tokenſKİı=SECRET",
    "?ſtoſken=SECRET",    # lead 內的 ſ（lead 字元類別不加 IGNORECASE）
    "?Kſ&toſken=SECRET",
    "?t\\oſ�ken=SECRET",
    "?TOſKEN=SECRET",
])
def test_explicit_folding_chars_inside_key_are_masked(text):
    out = lr.redact(text if "?" in text else "/p?a=1" + text)
    assert "SECRET" not in out, out
    assert out.endswith("=" + lr.MASK), out
    _check_superset(text)


@pytest.mark.parametrize("text", [
    "?ſtoken=S",    # 鍵名前面的 ſ 在 lead 內，鍵名本身仍要遮
    "?toke=S",           # 不是鍵
    "?toſ=S",       # 缺字母
    "?tokexſ=S",
])
def test_folding_chars_do_not_make_other_keys_match(text):
    if text == "?ſtoken=S":
        assert lr.redact(text) == "?ſtoken=" + lr.MASK
    else:
        assert lr.redact(text) == text


@pytest.mark.parametrize("fold", ["ſ", "K"])
@pytest.mark.parametrize("prefix", ["?acce", "?to", "?access", "?token", "&acceſ", "&toK", "&t"])
def test_runs_of_folding_chars_stay_linear(prefix, fold):
    # ſ 同時可以是 s 也可以是雜字元，K 同時可以是 k 也可以是雜字元：不能讓 regex 在這個歧義上多項式回溯
    for tail in ("", "x", "=", "token", "t", "a"):
        text = prefix + fold * N + tail
        start = time.perf_counter()
        lr._mask_loose_keys(text)
        lr.redact(text)
        assert time.perf_counter() - start < 0.05, (prefix, fold, tail)
    text = ("?" + "acce" + fold * 50) * (N // 54)
    start = time.perf_counter()
    lr.redact(text)
    assert time.perf_counter() - start < 0.05


def test_ignorecase_fold_audit_of_other_patterns():
    """其他 pattern（路徑、query、_url_word）只在「字母本身」用 IGNORECASE，折疊字元只會讓遮罩變多；
    字元類別裡沒有字母，不會被折疊字元排除。必要條件檢查也涵蓋折疊字元。"""
    # 路徑前綴：ı／İ 比對 i（多遮），ſ／K 不影響
    assert lr.redact("/prıvate_SECRET/mcp") == "/prıvate_" + lr.MASK + "/mcp"
    assert lr.redact("/PRİVATE_SECRET/mcp") == "/PRİVATE_" + lr.MASK + "/mcp"
    # query 鍵：K 比對 k、ſ 比對 s（必要條件檢查的 lower() 也把 K 變成 k）
    assert lr.redact("?Key=1&toKen=SECRET") == "?Key=1&toKen=" + lr.MASK
    assert lr.redact("?acceſſ_token=SECRET") == "?acceſſ_token=" + lr.MASK
    assert lr.redact("?access_toKen=SECRET") == "?access_toKen=" + lr.MASK
    # 只有 K 當 k 的鍵（沒有任何 ASCII k）也要通過必要條件檢查
    assert lr._has_candidate("?toKen=S")


def test_invalid_escape_detection_matches_per_run_reference():
    r = random.Random(20261113)
    for _ in range(20_000):
        text = _gen(r)
        assert lr._has_invalid_escape_in_url(text) == _ref_has_invalid_escape_in_url(text), repr(text)


@pytest.mark.parametrize("text", [
    "a%zz", "/a%zz", "/a %zz", "/a%41", "%zz/a", "x /%", "x /% y", "'/%'", '"a/b%2"', "a/b%2'", "/%4é", "é/%",
    "　/%", "a　/%", "/　%", "/a\n%zz", "http://x/%", "://%", "/" * 1000 + "%",
])
def test_invalid_escape_detection_special_cases_match_reference(text):
    assert lr._has_invalid_escape_in_url(text) == _ref_has_invalid_escape_in_url(text)


def test_unquote_is_identical_to_urllib_unquote_with_replace():
    r = random.Random(20261114)
    for _ in range(30_000):
        text = _gen(r)
        assert lr._unquote(text) == unquote(text, errors="replace"), repr(text)


@pytest.mark.parametrize("text", [
    "%e2%80", "%e2%80…", "é%a9", "%c3é", "%c3%a9é%a9", "%ed%a0%80", "\ud800%41", "%41\udc80", "%e2%80a",
    "%f0%9f%98", "%f0%9f%98%80", "%c0%af", "%e0%80%af", "%5cx41", "\\x41%41", "\\\\%41", "%00%0a", "%%41", "%4%41", "%41%",
    "%é", "%4é", "%\x80", "%" * 5 + "41", "%25" * 3, "%2541", "%e2%84%aa",
])
def test_unquote_special_cases_match_urllib(text):
    assert lr._unquote(text) == unquote(text, errors="replace")


# ---------------------------------------------------------------------------
# 沒有「每段一次」的 Python 迴圈：以 Python 層的 line event 數量當成決定性的代理指標
# （計時測試會受機器負載影響；每段一次的迴圈會讓 line event 與段數成正比）
# ---------------------------------------------------------------------------


def _python_lines(fn, *args) -> int:
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


def _segment_shapes(n: int) -> dict[str, str]:
    return {
        "/x?+&": "/x?" + "&" * n,
        "/x?+&=": "/x?" + "&=" * (n // 2),
        "/x?+&t": "/x?" + "&t" * (n // 2),
        "/x?token=a+&=": "/x?token=a" + "&=" * (n // 2),
        "/x?+_'": "/x?" + "_'" * (n // 2),
        "/x?%25+%": "/x?%25" + "%" * n,
        "%2_e%3b": "%2_e%3b" * (n // 7),
        "/x?+%25": "/x?" + "%25" * (n // 3),
        "/x?+;": "/x?" + ";" * n,
        "/x?+a=": "/x?" + "a=" * (n // 2),
        "/x?+%zz": "/x?" + "%zz" * (n // 3),
        "/x?+%41%": "/x?" + "%41%" * (n // 4),
        "slash-space": "/a " * (n // 3),
        "?é&": "?" + "é&" * (n // 2),
        "nested": "/x?" + ("%" + "25" * 15 + "a") * (n // 32),
    }


@pytest.mark.parametrize("name", list(_segment_shapes(1)))
def test_redact_python_work_does_not_grow_with_segment_count(name):
    small = _python_lines(lr.redact, _segment_shapes(N // 16)[name])
    big = _python_lines(lr.redact, _segment_shapes(N)[name])
    # 解碼回合最多 16 輪，每輪是固定行數；每段一次的迴圈在 64KB 會是數萬行。
    assert big < 1500, f"{name}: {big} python lines for 64KB"
    assert big <= small + 400, f"{name}: python lines grow with input: {small} -> {big}"


def test_decode_has_no_per_escape_python_work():
    text = "%2_e%3b" * 9000 + "%41" * 9000
    assert _python_lines(lr._unquote, text) < 40
    assert _python_lines(lr._mask_loose_keys, "?" + "&=" * 30_000) < 10
    assert _python_lines(lr._has_invalid_escape_in_url, "a/b " * 30_000) < 10


@pytest.mark.parametrize("name", list(_segment_shapes(1)))
def test_redact_segment_shapes_are_fast(name):
    text = _segment_shapes(N)[name]
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        lr.redact(text)
        best = min(best, time.perf_counter() - start)
    assert best < 0.05, f"{name}: {best * 1000:.1f} ms"


# ---------------------------------------------------------------------------
# uvicorn.access：先限制掃描窗再遮罩
# ---------------------------------------------------------------------------


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def access_logger():
    from uvicorn.logging import AccessFormatter

    lr.install()
    handler = _Lines()
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    logger = logging.getLogger("uvicorn.access")
    old = (logger.level, logger.propagate)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield logger, handler
    logger.removeHandler(handler)
    logger.setLevel(old[0])
    logger.propagate = old[1]


def _access_record(path):
    return logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1", "GET", path, "1.1", 200), None)


def _scrub_args(path) -> tuple:
    record = _access_record(path)
    lr.scrub_record(record)
    return record.args


@pytest.mark.parametrize("size", [8 * 1024, 64 * 1024, 1024 * 1024])
@pytest.mark.parametrize("unit", ["&=", "&", "%25", "%2_e%3b", "&t", "_'", "%"])
def test_access_path_masking_work_is_bounded_regardless_of_length(monkeypatch, size, unit):
    seen: list[int] = []
    real = lr.redact

    def counting(text):
        seen.append(len(text))
        return real(text)

    monkeypatch.setattr(lr, "redact", counting)
    path = "/x?" + (unit * (size // len(unit)))[:size]
    args = _scrub_args(path)
    assert seen and max(seen) <= lr.ACCESS_PATH_SCAN_CHARS + 200, seen
    assert len(args) == 5 and len(args[2]) <= lr.ACCESS_PATH_MAX_CHARS + lr.ACCESS_PATH_MARKER_SLACK


def test_access_path_full_record_cost_does_not_depend_on_path_length(access_logger):
    logger, handler = access_logger
    for unit in ("&=", "%2_e%3b", "%25", "&"):
        for size in (8 * 1024, 64 * 1024, 1024 * 1024):
            path = "/x?" + (unit * (size // len(unit)))[:size]
            best = float("inf")
            for _ in range(3):
                handler.lines.clear()
                start = time.perf_counter()
                logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", path, "1.1", 200)
                best = min(best, time.perf_counter() - start)
            assert len(handler.lines) == 1 and len(handler.lines[0]) < 2300
            assert best < 0.02, f"{unit!r} {size}: {best * 1000:.2f} ms"


def test_plain_long_access_path_marker_counts_every_dropped_char():
    path = "/" + "a" * 1_000_000
    shown = _scrub_args(path)[2]
    assert shown == path[:lr.ACCESS_PATH_MAX_CHARS] + f" …[truncated {len(path) - lr.ACCESS_PATH_MAX_CHARS} chars]"


@pytest.mark.parametrize("length", [1, 100, 2048, 2049, 2080, 3000, 4096])
def test_access_path_up_to_scan_window_is_exactly_the_old_result(length):
    for tail in ("", "?token=" + SECRET[:4]):
        path = ("/" + "a" * length)[:length - len(tail)] + tail
        shown = _scrub_args(path)[2]
        masked = lr.redact(path)
        if len(masked) <= lr.ACCESS_PATH_MAX_CHARS + lr.ACCESS_PATH_MARKER_SLACK:
            assert shown == masked


_PREFIXES = ("/private_", "?token=", "&access_token=", "/p%72ivate_", "%2Fprivate_", "/private\\_", ";to�ken=", "?to.ken%20=",
             "/%252570rivate_", "%5cprivate_", "/private�_", "?a=b&token=")


@pytest.mark.parametrize("anchor", ["scan", "show"])
def test_access_path_cut_points_never_leak_any_secret_char(anchor):
    """兩個切點（掃描窗 4096、顯示上限 2048）附近，憑證落在切點前、中、後，任何字元都不能出現。"""
    cut = lr.ACCESS_PATH_SCAN_CHARS if anchor == "scan" else lr.ACCESS_PATH_MAX_CHARS
    for prefix in _PREFIXES:
        for offset in range(-len(prefix) - 8, 10):
            start = cut - len(prefix) + offset
            for tail in ("", "/mcp", "%41", "&x=1", "%2"):
                path = "/" + "a" * start + prefix + SECRET + tail
                record = _access_record(path)
                lr.scrub_record(record)
                line = record.getMessage()
                assert not _leaked(line), (anchor, prefix, offset, tail, line[-100:])


def test_access_path_second_scrub_is_a_noop_at_cut_points():
    for prefix in _PREFIXES[:8]:
        for cut in (lr.ACCESS_PATH_MAX_CHARS, lr.ACCESS_PATH_SCAN_CHARS):
            for offset in range(-len(prefix) - 4, 6):
                path = "/" + "a" * (cut - len(prefix) + offset) + prefix + SECRET + "b" * 50
                record = _access_record(path)
                lr.scrub_record(record)
                first = record.args
                record.args = tuple(first)  # 新的 tuple：指紋對不上，一定會再遮
                lr.scrub_record(record)
                assert record.args == first, (prefix, cut, offset, first[2][-60:], record.args[2][-60:])


# ---------------------------------------------------------------------------
# R3-2、R3-3：截斷冪等（redact(redact(x)) == redact(x)）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["/private_", "?token=", "&access_token=", ";to.ken =", "%2fprivate_", "/p\\rivate_"])
def test_redact_truncation_is_idempotent_right_after_a_token(prefix):
    limit = lr.MAX_REDACT_CHARS - lr.TRUNCATE_RESERVE
    for offset in range(-len(prefix) - 6, 8):
        # 單一超長 URL（沒有空白可以退）：標記起點落在上限附近，值有一部分被切掉
        for body in (SECRET, "a", SECRET + "&x=1"):
            text = "a" * (limit - len(prefix) + offset) + prefix + body + "b" * 200
            once = lr.redact(text)
            assert not _leaked(once.replace("truncated", "").replace("chars", ""))
            assert lr.redact(once) == once, (prefix, offset, once[-60:], lr.redact(once)[-60:])


def test_redact_truncation_cut_inside_mask_does_not_split_it():
    # 遮罩讓字串變長，超過上限後的切點剛好落在 *** 之內
    unit = "/private_x "
    for pad in range(0, 40):
        text = "q" * pad + unit * 14_000
        once = lr.redact(text)
        assert lr.redact(once) == once, pad
        head = once.split(" …[truncated ")[0]
        assert not head.endswith(("*", "**")) or head.endswith("***"), head[-10:]


def test_truncation_marker_is_separated_from_the_masked_value():
    text = "a" * (lr.MAX_REDACT_CHARS - 20) + "/private_" + SECRET + "b" * 500
    once = lr.redact(text)
    assert " …[truncated " in once
    assert lr.redact(once) == once


def test_redact_truncation_fuzz_is_idempotent_near_the_cut():
    r = random.Random(7)
    limit = lr.MAX_REDACT_CHARS - lr.TRUNCATE_RESERVE
    for _ in range(300):
        prefix = r.choice(["/private_", "?token=", "&access_token=", "/x?to.ken%20=", "%2fprivate_", "a=b&token="])
        offset = r.randint(-len(prefix) - 12, 12)
        filler = r.choice(["a", "ab/", "/", "%41", "x?y=z&"])
        head = (filler * (limit // len(filler) + 2))[:limit - len(prefix) + offset]
        text = head + prefix + SECRET + r.choice(["", "/x", "&y=1", "***", "%"]) + "b" * r.randint(0, 300)
        once = lr.redact(text)
        assert lr.redact(once) == once, (prefix, offset, once[-50:])


# ---------------------------------------------------------------------------
# R3-1：單次遮罩的標記不可讓事後才出現的內容漏遮
# ---------------------------------------------------------------------------


def _format(record) -> str:
    return logging.Formatter("%(message)s").format(record)


def _scrub_then_format(record) -> str:
    lr.scrub_record(record)
    return _format(record)


@pytest.fixture
def factory_installed():
    lr.install()


def test_exc_info_assigned_after_factory_is_still_masked(factory_installed):
    logger = logging.getLogger("f10.excinfo")
    try:
        raise ValueError("boom /p?token=" + SECRET)
    except ValueError:
        ei = sys.exc_info()
    record = logger.makeRecord("f10.excinfo", logging.INFO, __file__, 1, "clean", (), None)
    record.exc_info = ei
    assert SECRET not in _scrub_then_format(record)


def test_make_log_record_with_only_exc_info_is_masked(factory_installed):
    try:
        raise ValueError("boom /p?token=" + SECRET)
    except ValueError:
        ei = sys.exc_info()
    for mapping in ({"exc_info": ei}, {"msg": "m", "exc_info": ei}):
        record = logging.makeLogRecord(mapping)
        assert SECRET not in _scrub_then_format(record), mapping


def test_exception_changed_after_scrub_is_masked_by_the_next_layer(factory_installed):
    """exc_info 有、exc_text 為空時不信任標記：每一層 handler 都重新格式化並遮 traceback。"""
    logger = logging.getLogger("f10.excmut")
    err = ValueError("clean")
    try:
        raise err
    except ValueError:
        ei = sys.exc_info()
    record = logger.makeRecord("f10.excmut", logging.INFO, __file__, 1, "clean", (), ei)
    lr.scrub_record(record)
    err.args = ("later /p?token=" + SECRET,)
    assert SECRET not in _scrub_then_format(record)


def test_lazy_arg_that_changes_after_scrub_is_masked(factory_installed):
    box = ["clean"]

    class Lazy:
        def __str__(self):
            return box[0]

    record = logging.getLogger("f10.lazy").makeRecord("f10.lazy", logging.INFO, __file__, 1, "v=%s", (Lazy(),), None)
    box[0] = "/p?token=" + SECRET
    assert SECRET not in _scrub_then_format(record)


def test_mutable_container_in_dict_args_changed_after_scrub_is_masked(factory_installed):
    box = ["clean"]

    class Lazy:
        def __str__(self):
            return box[0]

    args = {"a": [Lazy()]}
    record = logging.getLogger("f10.dict").makeRecord("f10.dict", logging.INFO, __file__, 1, "v=%(a)s", (args,), None)
    box[0] = "/p?token=" + SECRET
    assert SECRET not in _scrub_then_format(record)


def test_list_arg_mutated_after_scrub_is_masked(factory_installed):
    items = ["clean"]
    record = logging.getLogger("f10.list").makeRecord("f10.list", logging.INFO, __file__, 1, "v=%s", (items,), None)
    items[0] = "/p?token=" + SECRET
    assert SECRET not in _scrub_then_format(record)


def test_extra_still_cannot_forge_a_mark(factory_installed):
    logger = logging.getLogger("f10.extra")
    for name in ("_scrubbed", "scrubbed", "_mcp_admin_scrubbed", "redacted"):
        record = logger.makeRecord("f10.extra", logging.INFO, __file__, 1, "x", (), None, extra={name: True})
        record.msg = "/p?token=" + SECRET
        assert SECRET not in _scrub_then_format(record)


def test_plain_record_is_still_scrubbed_only_once(monkeypatch):
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    monkeypatch.setattr(lr, "_factory_installed", False)
    lr.install()
    calls: list[int] = []
    real = lr.redact

    def counting(text):
        calls.append(len(text))
        return real(text)

    monkeypatch.setattr(lr, "redact", counting)
    record = logging.getLogger("f10.once").makeRecord("f10.once", logging.INFO, __file__, 1, "GET /private_%s %d", (SECRET, 200), None)
    assert SECRET not in record.getMessage()
    first = len(calls)
    assert first >= 1
    lr.scrub_record(record)
    lr.PathTokenRedactionFilter().filter(record)
    assert len(calls) == first


def test_exception_record_with_cached_text_is_scrubbed_only_once(monkeypatch):
    """exc_text 已經快取（含憑證時遮好）的 record 仍可登記標記：重複 scrub 不再格式化 traceback。"""
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    monkeypatch.setattr(lr, "_factory_installed", False)
    lr.install()
    try:
        raise ValueError("boom /p?token=" + SECRET)
    except ValueError:
        ei = sys.exc_info()
    record = logging.getLogger("f10.exc2").makeRecord("f10.exc2", logging.INFO, __file__, 1, "m", (), ei)
    assert record.exc_text and SECRET not in record.exc_text
    calls: list[int] = []
    real = lr.redact
    monkeypatch.setattr(lr, "redact", lambda t: (calls.append(1), real(t))[1])
    lr.scrub_record(record)
    assert calls == []
