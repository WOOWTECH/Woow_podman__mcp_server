"""空白的 OD_CHAT_TIMEOUT 不得讓子行程起不來（2026-11-18：新帳號的 connection 全是空字串，子行程 exit 1 一直重啟）。"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from opendesign_mcp_server import drop_blank_chat_timeout


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_value_is_removed(value):
    env = {"OD_CHAT_TIMEOUT": value, "OD_API_BASE": ""}
    assert drop_blank_chat_timeout(env) is True
    assert "OD_CHAT_TIMEOUT" not in env
    assert env["OD_API_BASE"] == ""  # 只動 OD_CHAT_TIMEOUT


@pytest.mark.parametrize("env", [{"OD_CHAT_TIMEOUT": "120"}, {"OD_CHAT_TIMEOUT": " 45 "}, {}])
def test_set_or_missing_value_is_kept(env):
    before = dict(env)
    assert drop_blank_chat_timeout(env) is False
    assert env == before


@pytest.mark.parametrize("value, expected", [("", 300.0), ("120", 120.0)])
def test_package_imports_with_connection_values(value, expected):
    """在新行程 import（與子行程同一條路）：空字串用預設 300 秒、有值照用，都不會丟 ValueError。"""
    env = {**os.environ, "OD_CHAT_TIMEOUT": value, "OD_API_BASE": "", "OD_BASIC_USER": "", "OD_BASIC_PASSWORD": ""}
    code = "import opendesign_mcp_server as p; print(p._od_mcp_server.CHAT_TIMEOUT)"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-800:]
    assert float(result.stdout.strip().splitlines()[-1]) == expected
