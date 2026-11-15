"""log_redaction：path token 與 app password（含 HTTP Basic base64）都不進 log。"""
from __future__ import annotations

import base64
import io
import logging
import sys

import pytest
from uvicorn.logging import AccessFormatter

from nextcloud_mcp_admin import log_redaction

from .fake_nextcloud import LOGIN, PASSWORD


@pytest.fixture(autouse=True)
def _reset():
    log_redaction.install()
    log_redaction.set_secrets(None)
    yield
    log_redaction.set_secrets(None)


def test_path_token_is_redacted_in_access_log(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/1.1" %d', "1.2.3.4", "POST",
                                            "/private_abcDEF123/mcp", 200)
    assert "abcDEF123" not in caplog.text and "/private_***/mcp" in caplog.text


@pytest.mark.parametrize("logger_name", ["mcp_admin_core.process", "nextcloud_mcp_admin.x", "uvicorn.error", "root"])
def test_password_is_redacted_from_any_logger(caplog, logger_name):
    caplog.set_level(logging.DEBUG)
    log_redaction.set_secrets(PASSWORD, LOGIN)
    logging.getLogger(None if logger_name == "root" else logger_name).warning("[mcp-server] pw=%s end", PASSWORD)
    assert PASSWORD not in caplog.text and "pw=*** end" in caplog.text


def test_basic_auth_form_is_redacted(caplog):
    caplog.set_level(logging.INFO)
    log_redaction.set_secrets(PASSWORD, LOGIN)
    basic = base64.b64encode(f"{LOGIN}:{PASSWORD}".encode()).decode()
    logging.getLogger("httpx").info("Authorization: Basic %s", basic)
    assert basic not in caplog.text


def test_exception_text_is_redacted(caplog):
    caplog.set_level(logging.INFO)
    log_redaction.set_secrets(PASSWORD, LOGIN)
    try:
        raise RuntimeError(f"boom {PASSWORD}")
    except RuntimeError:
        logging.getLogger("nextcloud_mcp_admin").exception("failed")
    assert PASSWORD not in caplog.text


def test_short_or_empty_secret_is_not_registered():
    log_redaction.set_secrets("abc")
    assert log_redaction.registered_secret_count() == 0
    log_redaction.set_secrets("")
    assert log_redaction.registered_secret_count() == 0
    assert log_redaction.redact_secrets("abc") == "abc"


def test_clearing_secrets(caplog):
    caplog.set_level(logging.INFO)
    log_redaction.set_secrets(PASSWORD)
    log_redaction.set_secrets(None)
    logging.getLogger("x").info("value %s", "Sup3r")
    assert "value Sup3r" in caplog.text


def test_multiple_profiles_keep_all_masks_and_release_deleted_profile(caplog):
    caplog.set_level(logging.WARNING)
    other_password = "different-nextcloud-password"
    log_redaction.set_secrets(PASSWORD, LOGIN)
    log_redaction.set_profile_secrets("second", other_password, "second-user")
    try:
        logger = logging.getLogger("nextcloud_mcp_admin")
        logger.warning("default=%s other=%s", PASSWORD, other_password)
        assert PASSWORD not in caplog.text
        assert other_password not in caplog.text
        log_redaction.remove_profile_secrets("second")
        assert log_redaction.registered_secret_count() == 2
        logger.warning("default=%s", PASSWORD)
        assert PASSWORD not in caplog.text
    finally:
        log_redaction.remove_profile_secrets("second")


# -- uvicorn access log：args 必須維持 5 元組（2026-11-14 實測 F-2）-----------------------------------
#
# 請求網址帶著已登記的 app password（?api_token=<密碼>、?credential=<Basic base64>；這兩個鍵名不在核心 query 遮罩名單內，
# 由本模組接手）時，舊版 _scrub_record 把 record.args 設成 None；uvicorn 的 AccessFormatter 用
# ``client_addr, method, full_path, http_version, status_code = record.args`` 拆參數，結果 logging 內部錯誤
# （--- Logging error ---、約 45 行 TypeError），那一行 access log 消失。這裡用真的 AccessFormatter 與 uvicorn 實際的
# record 形狀（'%s - "%s %s HTTP/%s" %d' 與 5 個引數）驗證。

ACCESS_TEMPLATE = '%s - "%s %s HTTP/%s" %d'
UVICORN_ACCESS_FORMAT = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'  # uvicorn 的 LOGGING_CONFIG
TOKEN = "abcDEF123tokenTOKEN"


class _UvicornOutput(logging.StreamHandler):
    """uvicorn 容器 stdout 的實際輸出路徑（AccessFormatter）；logging 內部錯誤（--- Logging error ---）記在 ``errors``。"""

    def __init__(self) -> None:
        super().__init__(io.StringIO())
        self.errors: list[BaseException | None] = []
        self.setFormatter(AccessFormatter(UVICORN_ACCESS_FORMAT, use_colors=False))

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 — logging 的 hook 名稱
        self.errors.append(sys.exc_info()[1])

    @property
    def text(self) -> str:
        return self.stream.getvalue()


@pytest.fixture
def access_output(monkeypatch):
    logger = logging.getLogger("uvicorn.access")
    handler = _UvicornOutput()
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.INFO)
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def _basic(login: str = LOGIN, password: str = PASSWORD) -> str:
    return base64.b64encode(f"{login}:{password}".encode()).decode()


@pytest.mark.parametrize(
    "query",
    [
        lambda: f"api_token={PASSWORD}",
        lambda: f"credential={_basic()}",
        lambda: f"x=1&api_token={PASSWORD}&y=2",
    ],
    ids=["app-password", "basic-base64", "among-other-params"],
)
def test_access_log_with_a_registered_secret_formats_and_is_masked(access_output, query):
    """修正前：args=None → AccessFormatter 拋 TypeError（cannot unpack non-iterable NoneType object），那行 log 消失。"""
    log_redaction.set_secrets(PASSWORD, LOGIN)
    path = f"/private_{TOKEN}/mcp?{query()}"
    logging.getLogger("uvicorn.access").info(ACCESS_TEMPLATE, "10.42.0.7:51234", "POST", path, "1.1", 403)

    assert access_output.errors == [], access_output.errors  # 沒有 --- Logging error ---
    assert access_output.text.count("\n") == 1 and "403 Forbidden" in access_output.text  # 這行 access log 沒有消失
    for leaked in (PASSWORD, _basic(), TOKEN):
        assert leaked not in access_output.text
    assert "POST /private_***/mcp?" in access_output.text and "***" in access_output.text.split("?", 1)[1]


def test_access_log_masking_is_the_same_text_as_before(access_output):
    """遮罩文字與舊版逐字相同：舊版輸出是 redact_secrets(整句)；只是 args 不再被清成 None。"""
    log_redaction.set_secrets(PASSWORD, LOGIN)
    path = f"/files?api_token={PASSWORD}&x=1"
    args = ("10.42.0.7:51234", "GET", path, "1.1", 200)
    logging.getLogger("uvicorn.access").info(ACCESS_TEMPLATE, *args)
    expected_path = log_redaction.redact_secrets(path)
    assert expected_path == "/files?api_token=***&x=1"
    assert access_output.text.strip() == f'INFO:     10.42.0.7:51234 - "GET {expected_path} HTTP/1.1" 200 OK'


def test_access_log_without_a_secret_in_the_url_is_untouched(access_output):
    log_redaction.set_secrets(PASSWORD, LOGIN)
    logging.getLogger("uvicorn.access").info(ACCESS_TEMPLATE, "10.42.0.7:51234", "GET", "/healthz", "1.1", 200)
    assert access_output.errors == []
    assert access_output.text.strip() == 'INFO:     10.42.0.7:51234 - "GET /healthz HTTP/1.1" 200 OK'


def _record(msg, args, name="x"):
    """經過已安裝的 record factory（就是 logging 每筆 record 實際走的路徑）建立 record。"""
    return logging.getLogRecordFactory()(name, logging.INFO, __file__, 1, msg, args, None)


def test_args_keep_their_shape_and_types_when_masked():
    log_redaction.set_secrets(PASSWORD, LOGIN)
    record = _record("%s %d %s %.1f", (f"pw={PASSWORD}", 7, "ok", 1.5))
    assert type(record.args) is tuple and len(record.args) == 4
    assert [type(a) for a in record.args] == [str, int, str, float]
    assert record.args == ("pw=***", 7, "ok", 1.5)
    assert record.getMessage() == "pw=*** 7 ok 1.5"


def test_access_shape_is_kept_for_the_five_uvicorn_arguments():
    log_redaction.set_secrets(PASSWORD, LOGIN)
    record = _record(ACCESS_TEMPLATE, ("1.2.3.4:5", "POST", f"/x?api_token={PASSWORD}", "1.1", 200), "uvicorn.access")
    assert isinstance(record.args, tuple) and len(record.args) == 5
    assert record.args[4] == 200 and isinstance(record.args[4], int)
    assert record.args[2] == "/x?api_token=***"
    assert PASSWORD not in record.getMessage()


def test_dict_args_are_masked_value_by_value():
    log_redaction.set_secrets(PASSWORD, LOGIN)
    record = _record("%(who)s sent %(pw)s (%(n)d)", {"who": "alice", "pw": PASSWORD, "n": 3})
    assert isinstance(record.args, dict) and set(record.args) == {"who", "pw", "n"}
    assert record.args == {"who": "alice", "pw": "***", "n": 3}
    assert record.getMessage() == "alice sent *** (3)"


@pytest.mark.parametrize("args", [None, (), {}], ids=["None", "empty-tuple", "empty-dict"])
def test_records_without_args_are_masked_and_do_not_fail(args):
    log_redaction.set_secrets(PASSWORD, LOGIN)
    record = _record(f"login failed for {LOGIN} with {PASSWORD}", args)
    assert record.getMessage() == f"login failed for {LOGIN} with ***"
    assert not record.args  # 沒有 args 的紀錄維持沒有 args（舊版是把 args 設成 None；None／空 tuple 都不會讓 logging 出錯）
    clean = _record("nothing to hide", args)
    assert clean.getMessage() == "nothing to hide"


def test_secret_in_the_template_and_non_string_objects_are_masked():
    log_redaction.set_secrets(PASSWORD, LOGIN)
    template = _record(f"token {PASSWORD} for %s", ("alice",))
    assert template.getMessage() == "token *** for alice" and template.args == ("alice",)
    error = _record("request failed: %s", (RuntimeError(f"HTTP 401 for {PASSWORD}"),))
    assert PASSWORD not in error.getMessage() and error.getMessage() == "request failed: HTTP 401 for ***"
    number = _record("%s %d", (PASSWORD, 5))
    assert number.args == ("***", 5) and isinstance(number.args[1], int)


def test_secret_split_across_two_arguments_is_still_masked():
    """秘密被拆在相鄰兩個引數之間：逐一遮罩遮不到，改用整句遮罩（與舊版相同的輸出）。"""
    log_redaction.set_secrets(PASSWORD, LOGIN)
    record = _record("%s%s", (PASSWORD[:8], PASSWORD[8:]))
    assert PASSWORD not in record.getMessage() and record.getMessage() == "***"
    access = _record(ACCESS_TEMPLATE, (PASSWORD[:8], "GET", PASSWORD[8:], "1.1", 200), "uvicorn.access")
    assert isinstance(access.args, tuple) and len(access.args) == 5  # 即使如此 access log 的形狀也不變
    assert PASSWORD not in "".join(str(a) for a in access.args)


@pytest.mark.parametrize(
    ("msg", "args"),
    [
        ("[mcp-server] pw=%s end", (PASSWORD,)),
        ("%s - %s", (PASSWORD, f"Authorization: Basic {_basic()}")),
        ("%(a)s/%(b)s", {"a": PASSWORD, "b": "z"}),
        (f"plain {PASSWORD}", ()),
        (f"plain {PASSWORD}", None),
        ("no secret %s", ("x",)),
        ("%s", (f"{LOGIN}:{PASSWORD}",)),
    ],
)
def test_masked_text_is_the_same_as_masking_the_whole_message(msg, args):
    """遮罩輸出逐字不變：結果等於「先格式化整句、再遮罩」（舊版的作法）。"""
    log_redaction.set_secrets(PASSWORD, LOGIN)
    expected = log_redaction.redact_secrets(msg % args if args else msg)
    assert _record(msg, args).getMessage() == expected


def test_masking_is_idempotent():
    log_redaction.set_secrets(PASSWORD, LOGIN)
    record = _record(ACCESS_TEMPLATE, ("1.2.3.4:5", "POST", f"/x?api_token={PASSWORD}", "1.1", 200), "uvicorn.access")
    once = (record.msg, record.args, record.getMessage())
    log_redaction._scrub_record(record)
    assert (record.msg, record.args, record.getMessage()) == once
    plain = _record("%s", (f"v={PASSWORD}",))
    first = (plain.msg, plain.args)
    log_redaction._scrub_record(plain)
    assert (plain.msg, plain.args) == first
