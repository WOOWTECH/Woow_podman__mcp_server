"""nextcloud-mcp-server launcher：固定 Streamable HTTP、只綁 127.0.0.1、Tools 政策 → gating env、祕密不進 argv。"""
from __future__ import annotations

import json
import logging
import os

import pytest

from mcp_admin_core.process import _build_subprocess_env
from nextcloud_mcp_admin import launcher, tool_registry
from nextcloud_mcp_server.settings import load_settings
from nextcloud_mcp_server.tools import enabled_tools

from .fake_nextcloud import BASE_URL, LOGIN, PASSWORD

WRITE = ["create_text_file", "update_text_file", "upload_file"]
DELETE = ["delete_file_checked"]
ALL = tool_registry.get_all_tool_names()

SEED_MCP_SERVER = {
    "command": "nextcloud-mcp-server",
    "args": ["--port", "8000"],
    "port": 8000,
    "env": {},
    "env_prefix": "NEXTCLOUD_MCP_",
}


def _argv(argv):
    out = launcher.build_server_argv(argv)
    return dict(zip(out[::2], out[1::2]))


def _write_policy(tmp_path, disabled, **extra):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tools": {"disabled": disabled, **extra}}))
    return path


# -- argv ------------------------------------------------------------------------


def test_defaults_match_litellm_port():
    assert _argv([]) == {"--transport": "http", "--host": "127.0.0.1", "--port": "8000", "--path": "/mcp"}


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])
def test_non_loopback_host_is_ignored(host):
    assert _argv(["--host", host])["--host"] == "127.0.0.1"


def test_source_repo_gateway_args_are_accepted():
    got = _argv(["--transport", "http", "--host", "127.0.0.1", "--port", "3000", "--path", "mcp"])
    assert got == {"--transport": "http", "--host": "127.0.0.1", "--port": "3000", "--path": "/mcp"}


@pytest.mark.parametrize("transport", ["stdio", "sse"])
def test_other_transports_are_overridden(transport):
    assert _argv(["--transport", transport])["--transport"] == "http"


def test_vendored_parser_accepts_generated_argv():
    from nextcloud_mcp_server.server import build_parser

    args = build_parser().parse_args(launcher.build_server_argv(["--port", "8123"]))
    assert (args.transport, args.host, args.port, args.path) == ("http", "127.0.0.1", 8123, "/mcp")


# -- policy → env -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("disabled", "expected"),
    [
        # 出廠：只開讀取
        (WRITE + DELETE, {"NEXTCLOUD_MCP_READONLY": "true", "NEXTCLOUD_MCP_ALLOW_DELETE": "false",
                          "NEXTCLOUD_MCP_DISABLED_TOOLS": ",".join(WRITE + DELETE)}),
        # 全開
        ([], {"NEXTCLOUD_MCP_READONLY": "false", "NEXTCLOUD_MCP_ALLOW_DELETE": "true",
              "NEXTCLOUD_MCP_DISABLED_TOOLS": ""}),
        # 只開一支寫入
        (["update_text_file", "upload_file", "delete_file_checked"],
         {"NEXTCLOUD_MCP_READONLY": "false", "NEXTCLOUD_MCP_ALLOW_DELETE": "false",
          "NEXTCLOUD_MCP_DISABLED_TOOLS": "update_text_file,upload_file,delete_file_checked"}),
        # 只開刪除（寫入全關）：READONLY 必須 false 才會註冊刪除，寫入靠 DISABLED_TOOLS 關掉
        (WRITE, {"NEXTCLOUD_MCP_READONLY": "false", "NEXTCLOUD_MCP_ALLOW_DELETE": "true",
                 "NEXTCLOUD_MCP_DISABLED_TOOLS": ",".join(WRITE)}),
        # 停用讀取工具＋未知名稱（未知名稱略過）
        (["list_tasks", "nope", "get_file_tree"] + WRITE + DELETE,
         {"NEXTCLOUD_MCP_READONLY": "true", "NEXTCLOUD_MCP_ALLOW_DELETE": "false",
          "NEXTCLOUD_MCP_DISABLED_TOOLS": ",".join(["get_file_tree", "list_tasks"] + WRITE + DELETE)}),
    ],
)
def test_gating_env(disabled, expected):
    assert launcher.gating_env(disabled) == expected


@pytest.mark.parametrize(
    "disabled",
    [WRITE + DELETE, [], WRITE, DELETE, ["create_text_file"], ["get_file_content", "list_calendars"] + DELETE,
     ALL],
)
def test_child_registers_exactly_the_enabled_tools(disabled, monkeypatch):
    """launcher 算出的 env 交給 vendored Settings 後，子行程註冊的工具＝政策開啟的工具。"""
    for key, value in launcher.gating_env(disabled).items():
        monkeypatch.setenv(key, value)
    settings = load_settings(base_url=BASE_URL, username=LOGIN, app_password=PASSWORD)
    assert enabled_tools(settings) == [n for n in ALL if n not in disabled]


def test_policy_env_reads_config(tmp_path):
    path = _write_policy(tmp_path, ["delete_file_checked"])
    assert launcher.policy_env(path) == {
        "NEXTCLOUD_MCP_READONLY": "false",
        "NEXTCLOUD_MCP_ALLOW_DELETE": "false",
        "NEXTCLOUD_MCP_DISABLED_TOOLS": "delete_file_checked",
    }


def test_policy_env_accepts_comma_string(tmp_path):
    path = _write_policy(tmp_path, "create_text_file, upload_file")
    assert launcher.policy_env(path)["NEXTCLOUD_MCP_DISABLED_TOOLS"] == "create_text_file,upload_file"


@pytest.mark.parametrize("content", [None, "{not json", "[]", '{"tools": "x"}'])
def test_policy_env_fails_closed(tmp_path, content):
    path = tmp_path / "config.json"
    if content is not None:
        path.write_text(content)
    env = launcher.policy_env(path)
    assert env["NEXTCLOUD_MCP_READONLY"] == "true" and env["NEXTCLOUD_MCP_ALLOW_DELETE"] == "false"


def test_policy_env_uses_mcp_admin_config(tmp_path, monkeypatch):
    path = _write_policy(tmp_path, [])
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
    assert launcher.policy_env()["NEXTCLOUD_MCP_ALLOW_DELETE"] == "true"


def test_policy_overrides_manual_env(tmp_path, monkeypatch):
    """mcp_server.env 手動填的同名鍵不算數：政策（Tools 頁）是唯一來源。"""
    monkeypatch.setenv("NEXTCLOUD_MCP_READONLY", "false")
    monkeypatch.setenv("NEXTCLOUD_MCP_ALLOW_DELETE", "true")
    monkeypatch.setenv("NEXTCLOUD_MCP_DISABLED_TOOLS", "")
    launcher.apply_policy_env(_write_policy(tmp_path, WRITE + DELETE))
    assert os.environ["NEXTCLOUD_MCP_READONLY"] == "true"
    assert os.environ["NEXTCLOUD_MCP_ALLOW_DELETE"] == "false"
    assert os.environ["NEXTCLOUD_MCP_DISABLED_TOOLS"] == ",".join(WRITE + DELETE)


# -- connection env（core 注入）與祕密 ------------------------------------------------


def test_core_injects_connection_as_prefixed_env_and_not_argv():
    conn = {"base_url": BASE_URL, "username": LOGIN, "app_password": PASSWORD}
    env = _build_subprocess_env(conn, SEED_MCP_SERVER)
    assert env["NEXTCLOUD_MCP_BASE_URL"] == BASE_URL
    assert env["NEXTCLOUD_MCP_USERNAME"] == LOGIN
    assert env["NEXTCLOUD_MCP_APP_PASSWORD"] == PASSWORD
    argv = [SEED_MCP_SERVER["command"], *SEED_MCP_SERVER["args"]]
    server_argv = launcher.build_server_argv(SEED_MCP_SERVER["args"])
    for arg in argv + server_argv:
        assert PASSWORD not in arg and LOGIN not in arg


def test_main_hands_off_to_vendored_server(monkeypatch, tmp_path, capsys):
    from nextcloud_mcp_server import server

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(_write_policy(tmp_path, DELETE)))
    monkeypatch.setenv("NEXTCLOUD_MCP_APP_PASSWORD", PASSWORD)
    monkeypatch.delenv("FASTMCP_SHOW_SERVER_BANNER", raising=False)
    monkeypatch.setenv("FASTMCP_CHECK_FOR_UPDATES", "stable")
    captured = {}
    calls = []

    def fake_main(argv):
        calls.append("server")
        captured["argv"] = list(argv)
        captured["env"] = {k: os.environ.get(k) for k in launcher.GATING_ENV_KEYS}
        return 0

    # 請求內容上限 16 MiB（request_body.install 改 SDK transport 的預設值）要在 server 建 transport 之前裝好；
    # 這裡記錄呼叫順序、不真的改全域類別（真的效果見 core tests/test_child_request_body_limit.py）。
    monkeypatch.setattr(launcher.request_body, "install", lambda: calls.append("install"))
    monkeypatch.setattr(server, "main", fake_main)
    with pytest.raises(SystemExit) as exc:
        launcher.main(["--port", "8123", "--host", "0.0.0.0", "--transport", "stdio"])
    assert exc.value.code == 0
    assert calls == ["install", "server"]
    argv = captured["argv"]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "8123"
    assert argv[argv.index("--transport") + 1] == "http"
    assert captured["env"] == {
        "NEXTCLOUD_MCP_READONLY": "false",
        "NEXTCLOUD_MCP_ALLOW_DELETE": "false",
        "NEXTCLOUD_MCP_DISABLED_TOOLS": "delete_file_checked",
    }
    assert os.environ["FASTMCP_SHOW_SERVER_BANNER"] == "false"
    assert os.environ["FASTMCP_CHECK_FOR_UPDATES"] == "stable"  # 部署端覆寫要尊重
    err = capsys.readouterr().err
    assert "NEXTCLOUD_MCP_READONLY=false" in err and PASSWORD not in err


def test_main_propagates_vendored_exit_code(monkeypatch, tmp_path):
    """缺設定時 vendored main 回 2；launcher 要以非 0 結束（core 看門狗才會退避重試並顯示原因）。"""
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(_write_policy(tmp_path, [])))
    monkeypatch.setattr(launcher.request_body, "install", lambda: None)  # 不在測試行程裡改 SDK 全域類別
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 2


def test_launcher_import_does_not_load_fastmcp():
    """FastMCP 在 import 時讀 FASTMCP_*；launcher 頂層若先載入 fastmcp，banner／PyPI 版本檢查就關不掉。"""
    import subprocess
    import sys

    code = "import sys, nextcloud_mcp_admin.launcher; sys.exit(1 if 'fastmcp' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_quiet_url_loggers():
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    launcher.quiet_url_loggers()
    for name in ("httpx", "httpcore"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


def test_console_script_points_at_launcher():
    from importlib.metadata import entry_points

    eps = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert eps["nextcloud-mcp-server"] == "nextcloud_mcp_admin.launcher:main"
