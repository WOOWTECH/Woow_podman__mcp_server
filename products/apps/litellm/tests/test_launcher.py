"""litellm-mcp-server launcher：固定 Streamable HTTP、只綁 127.0.0.1、log 衛生。"""
from __future__ import annotations

import logging
import os
import sys

import pytest

from litellm_mcp_admin import launcher


def _argv(argv):
    out = launcher.build_server_argv(argv)
    return dict(zip(out[::2], out[1::2]))


def test_defaults():
    assert _argv([]) == {
        "--transport": "http",
        "--host": "127.0.0.1",
        "--port": "8000",
        "--path": "/mcp",
        "--log-level": "INFO",
    }


def test_port_and_path():
    got = _argv(["--port", "3000", "--path", "mcp/"])
    assert got["--port"] == "3000" and got["--path"] == "/mcp/"


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])
def test_non_loopback_host_is_ignored(host):
    assert _argv(["--host", host])["--host"] == "127.0.0.1"


def test_legacy_seed_args_are_accepted():
    # 來源 repo chart 的 args：--transport http --host 127.0.0.1 --port 3000 --path /mcp/
    got = _argv(["--transport", "http", "--host", "127.0.0.1", "--port", "3000", "--path", "/mcp/"])
    assert got == {"--transport": "http", "--host": "127.0.0.1", "--port": "3000", "--path": "/mcp/", "--log-level": "INFO"}


def test_stdio_transport_is_overridden():
    assert _argv(["--transport", "stdio"])["--transport"] == "http"


def test_quiet_url_loggers():
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    launcher.quiet_url_loggers()
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


def test_fastmcp_env_defaults_are_setdefault(monkeypatch):
    monkeypatch.delenv("FASTMCP_SHOW_SERVER_BANNER", raising=False)
    monkeypatch.setenv("FASTMCP_CHECK_FOR_UPDATES", "stable")
    launcher.apply_fastmcp_env_defaults()
    assert os.environ["FASTMCP_SHOW_SERVER_BANNER"] == "false"
    assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "stable"  # 部署端覆寫要尊重


def test_main_hands_off_to_vendored_server(monkeypatch):
    from woow_litellm_mcp_server import server

    captured = {}
    calls = []
    # 請求內容上限 16 MiB（request_body.install 改 SDK transport 的預設值）要在 server 建 transport 之前裝好；
    # 這裡記錄呼叫順序、不真的改全域類別（真的效果見 core tests/test_child_request_body_limit.py）。
    monkeypatch.setattr(launcher.request_body, "install", lambda: calls.append("install"))
    monkeypatch.setattr(server, "main", lambda: (calls.append("server"),
                                                 captured.setdefault("argv", list(sys.argv))))
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    launcher.main(["--port", "8123", "--host", "0.0.0.0"])
    assert calls == ["install", "server"]
    argv = captured["argv"]
    assert argv[0] == "litellm-mcp-server"
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "8123"
