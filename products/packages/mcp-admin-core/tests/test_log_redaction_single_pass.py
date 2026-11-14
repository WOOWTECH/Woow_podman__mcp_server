"""每筆 LogRecord 在 factory＋filter 路徑上只遮一次（r2co-1、r2co-4）；所有憑證皆合成值。"""
from __future__ import annotations

import logging

import pytest

from mcp_admin_core import log_redaction as lr

SECRET = "QZJWXKQZJWXK"


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


@pytest.fixture
def core_only_factory(monkeypatch):
    """只有共用層的 record factory（整個 core suite 內其他測試會讓 app 的 factory 疊在上面，次數就不是 1）。"""
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    monkeypatch.setattr(lr, "_factory_installed", False)
    lr.install()
    assert getattr(logging.getLogRecordFactory(), "_mcp_admin_url_token_scrubber", False)


@pytest.fixture
def redact_calls(monkeypatch):
    calls: list[int] = []
    real = lr.redact

    def counting(text):
        calls.append(len(text))
        return real(text)

    monkeypatch.setattr(lr, "redact", counting)
    return calls


def test_access_record_is_redacted_at_most_twice_in_full_path(core_only_factory, access_logger, redact_calls):
    """原本 factory 與 filter 各遮一次，每次遮 path 與整句，共 4 次；現在 path 與整句檢查各 1 次。"""
    logger, handler = access_logger
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/x?" + "&" * 50000, "1.1", 404)
    assert len(redact_calls) <= 2, redact_calls
    assert len(handler.lines) == 1


def test_second_scrub_of_same_record_is_a_noop(core_only_factory, redact_calls):
    record = logging.getLogger("t.single").makeRecord("t.single", logging.INFO, __file__, 1, "GET /private_%s", (SECRET,), None)
    lr.scrub_record(record)
    first = len(redact_calls)
    assert first >= 1
    lr.scrub_record(record)
    lr.PathTokenRedactionFilter().filter(record)
    assert len(redact_calls) == first


def test_plain_record_built_without_factory_is_still_masked_by_filter(access_logger):
    logger, handler = access_logger
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:1", "GET", "/private_" + SECRET + "/mcp", "1.1", 200), None)
    logger.handle(record)
    assert handler.lines and SECRET not in handler.lines[0]


def test_extra_with_marker_like_names_cannot_skip_redaction(access_logger):
    """用 extra 傳各種可能的『已遮罩』屬性名稱，record 照樣被遮（標記不在 record 屬性上）。"""
    logger, handler = access_logger
    handler.setFormatter(logging.Formatter("%(message)s"))
    names = ["_mcp_admin_scrubbed", "_scrubbed", "scrubbed", "redacted", "_redacted", "_mcp_admin_url_token_scrubber"]
    for name in names:
        handler.lines.clear()
        logger.info("seen /private_%s/mcp", SECRET, extra={name: True})
        assert handler.lines and SECRET not in handler.lines[0], name
    # 手動建立、帶同名屬性且不經 factory 的 record 也一樣
    for name in names:
        handler.lines.clear()
        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "seen /private_%s", (SECRET,), None)
        setattr(record, name, True)
        logger.handle(record)
        assert SECRET not in handler.lines[0], name
    assert not any(hasattr(logging.makeLogRecord({}), n) for n in names)


def test_record_mutated_after_scrub_is_scrubbed_again():
    record = logging.getLogger("t.mut").makeRecord("t.mut", logging.INFO, __file__, 1, "clean line", (), None)
    lr.scrub_record(record)
    record.msg = "now /private_" + SECRET + "/mcp"
    lr.scrub_record(record)
    assert SECRET not in record.getMessage()
    record.args = ()
    record.msg = "again ?token=" + SECRET
    lr.scrub_record(record)
    assert SECRET not in record.getMessage()


def test_pickle_and_copy_of_record_do_not_carry_a_skip_mark():
    import copy
    import pickle

    record = logging.getLogger("t.copy").makeRecord("t.copy", logging.INFO, __file__, 1, "clean line", (), None)
    lr.scrub_record(record)
    assert "_scrubbed" not in record.__dict__ and not any("scrub" in key for key in record.__dict__)
    clone = pickle.loads(pickle.dumps(record))
    clone.msg = "x /private_" + SECRET
    lr.scrub_record(clone)
    assert SECRET not in clone.getMessage()
    twin = copy.copy(record)
    twin.msg = "x /private_" + SECRET
    lr.scrub_record(twin)
    assert SECRET not in twin.getMessage()


def test_oversize_message_is_truncated_exactly_once(access_logger):
    """r2co-4：>128KiB 的訊息過 factory 與 filter 後只有一個標記，且計數對得上。"""
    logger, handler = access_logger
    handler.setFormatter(logging.Formatter("%(message)s"))
    original = " ".join("ab" for _ in range(120_000))
    assert len(original) > lr.MAX_REDACT_CHARS
    other = logging.getLogger("uvicorn.access")
    other.info("%s", original)  # 非 5 元組：走一般路徑，factory 與 filter 都會看到
    out = handler.lines[0]
    assert out.count("…[truncated ") == 1, out[-120:]
    head, tail = out.split(" …[truncated ")
    dropped = int(tail.split(" ")[0])
    assert len(head) + dropped == len(original)
    assert len(out) <= lr.MAX_REDACT_CHARS


def test_redact_output_of_truncation_is_stable_under_second_redact():
    original = " ".join("ab" for _ in range(120_000))
    once = lr.redact(original)
    assert lr.redact(once) == once
    huge = "x" * (lr.MAX_REDACT_CHARS + 5000)
    once = lr.redact(huge)
    assert lr.redact(once) == once and once.count("[truncated") == 1


def test_app_style_factory_that_rebuilds_args_does_not_truncate_twice():
    """emqx 等 app 的 factory 會重建 msg／args 後再呼叫 scrub_record：重複遮罩必須冪等（計數與標記正確）。"""
    path = "/" + "a" * 40000
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1", "GET", path, "1.1", 200), None)
    for _ in range(3):
        record.args = tuple(record.args)  # 新的 tuple 物件：指紋對不上，一定會再遮一次
        record.args = (*record.args[:2], str(record.args[2]), *record.args[3:])
        lr.scrub_record(record)
    shown = record.args[2]
    assert shown == path[:lr.ACCESS_PATH_MAX_CHARS] + f" …[truncated {len(path) - lr.ACCESS_PATH_MAX_CHARS} chars]"
    assert shown.count("[truncated") == 1


def test_redaction_that_grows_the_text_is_still_stable_under_repeat():
    text = "/private_x " * 14_000  # 遮罩後每段多 2 字元，總長超過上限
    once = lr.redact(text)
    assert len(once) <= lr.MAX_REDACT_CHARS
    assert lr.redact(once) == once
    assert once.count("[truncated") == 1


def test_mutating_dict_args_in_place_after_scrub_triggers_rescrub(core_only_factory):
    """第三方 filter 就地改動 dict args（身分不變）時，指紋也看內容，所以會重遮（驗證者 P3）。"""
    args = {"u": "/ok"}
    record = logging.getLogger("f7.dictargs").makeRecord("f7.dictargs", logging.INFO, __file__, 1, "%(u)s", (args,), None)
    assert record.getMessage() == "/ok"
    record.args["u"] = f"/private_{SECRET}/mcp"
    lr.scrub_record(record)
    assert SECRET not in record.getMessage()
