import logging
import sys

from uvicorn.logging import AccessFormatter

from headscale_mcp_admin.log_redaction import install, set_secrets


def test_access_formatter_preserves_arguments_and_redacts_credentials():
    install()
    set_secrets("fixture-upstream-credential")
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/fixture-upstream-credential", "1.1", 200), None)
    text = AccessFormatter('%(client_addr)s %(request_line)s %(status_code)s', use_colors=False).format(record)
    assert len(record.args) == 5 and record.args[-1] == 200
    assert "fixture-upstream-credential" not in text
    assert "[credential]" in text and "200" in text


def test_exception_credentials_are_redacted_before_handlers():
    install()
    set_secrets("fixture-error-credential")
    try:
        raise RuntimeError("upstream fixture-error-credential failed")
    except RuntimeError:
        record = logging.getLogger("headscale").makeRecord(
            "headscale", logging.ERROR, __file__, 1, "request failed", (), sys.exc_info())
    text = logging.Formatter().format(record)
    assert "fixture-error-credential" not in text
    assert "[credential]" in text
