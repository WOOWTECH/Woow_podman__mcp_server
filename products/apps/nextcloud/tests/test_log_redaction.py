"""log_redaction：path token 與 app password（含 HTTP Basic base64）都不進 log。"""
from __future__ import annotations

import base64
import logging

import pytest

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
