"""Session Timeout 的實際粒度要講清楚（商用驗收 DATA-NMCP-003-3）。

實測：Session Timeout 設 60 秒、Max Sessions 設 1，閒置 66 秒的 session 仍可用，約 307 秒才過期並釋放名額。
原因在上游 n8n-mcp 2.96.2（與 2.92.1 相同）：``dist/http-server-single-session.js`` 寫死
``SESSION_CLEANUP_INTERVAL = 5 * 60 * 1000``，過期 session 只在每 5 分鐘一次的清掃時移除（收到請求時不檢查），
而且 ``SESSION_TIMEOUT_MINUTES`` 以 parseInt 讀取，只能是整數分鐘。上游沒有環境變數可以調短清掃週期。

所以本 repo 能做的是：(1) 管理頁欄位說明寫清楚「約每 5 分鐘清掃、最小有效粒度約 5 分鐘」；(2) 映像建置時
驗證上游的清掃週期仍是我們寫在說明裡的值，上游一改就在 CI 失敗，提醒同步改說明。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from n8n_mcp_admin import child_env

APP_DIR = Path(__file__).resolve().parents[1]
SWEEP_LINE = "const SESSION_CLEANUP_INTERVAL = 5 * 60 * 1000;\n"


def _fake_pkg(tmp_path: Path, line: str | None) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    if line is not None:
        (dist / "http-server-single-session.js").write_text(
            "const MAX_SESSIONS = 1;\n" + line + "const STREAMABLE_HTTP_KEEP_ALIVE_MS = 15000;\n"
        )
    return tmp_path


def test_documented_sweep_is_five_minutes():
    assert child_env.UPSTREAM_SESSION_CLEANUP_SECONDS == 300


def test_reads_upstream_sweep_interval(tmp_path):
    assert child_env.upstream_session_cleanup_seconds(str(_fake_pkg(tmp_path, SWEEP_LINE))) == 300


def test_verify_passes_when_upstream_matches(tmp_path):
    assert child_env.verify_upstream_session_sweep(str(_fake_pkg(tmp_path, SWEEP_LINE))) == 0


def test_verify_fails_when_upstream_changes_interval(tmp_path):
    pkg = _fake_pkg(tmp_path, "const SESSION_CLEANUP_INTERVAL = 1 * 60 * 1000;\n")
    assert child_env.upstream_session_cleanup_seconds(str(pkg)) == 60
    assert child_env.verify_upstream_session_sweep(str(pkg)) == 1


def test_verify_fails_when_constant_disappears(tmp_path):
    pkg = _fake_pkg(tmp_path, "const SESSION_CLEANUP_INTERVAL = Number(process.env.X || 300000);\n")
    assert child_env.upstream_session_cleanup_seconds(str(pkg)) is None
    assert child_env.verify_upstream_session_sweep(str(pkg)) == 1
    assert child_env.verify_upstream_session_sweep(str(_fake_pkg(tmp_path / "missing", None))) == 1


def test_real_upstream_package_when_available():
    pkg = os.environ.get("N8N_MCP_PACKAGE_DIR")
    if not pkg or not os.path.isdir(pkg):
        pytest.skip("n8n-mcp 套件不在本機（映像建置時 Dockerfile 會驗證）")
    assert child_env.upstream_session_cleanup_seconds(pkg) == child_env.UPSTREAM_SESSION_CLEANUP_SECONDS


def test_dockerfile_verifies_upstream_sweep():
    dockerfile = (APP_DIR / "Dockerfile").read_text()
    assert "verify_upstream_session_sweep" in dockerfile


def _field_line(name: str) -> str:
    src = (APP_DIR / "frontend" / "src" / "appConfig.jsx").read_text()
    return next(line for line in src.splitlines() if f"name: '{name}'" in line)


def test_session_timeout_field_explains_granularity():
    line = _field_line("mcp_session_timeout")
    help_text = re.search(r"help: '([^']*)'", line)
    assert help_text, "Session Timeout 欄位要有 help 說明"
    text = help_text.group(1)
    assert "5 分鐘" in text  # 清掃週期／最小有效粒度
    assert "分鐘" in text and "進位" in text  # 秒數會無條件進位成分鐘


def test_max_sessions_field_explains_how_to_free_slots():
    line = _field_line("mcp_max_sessions")
    help_text = re.search(r"help: '([^']*)'", line)
    assert help_text, "Max Sessions 欄位要有 help 說明"
    text = help_text.group(1)
    assert "429" in text
    assert "Restart" in text  # 立即清掉崩潰用戶端留下的 session 的方法
