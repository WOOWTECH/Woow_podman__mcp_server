"""n8n 的 session 名額：閘道回收（mcp_admin_core.session_reclaim）依賴的上游行為與預設值。

上游 n8n-mcp 2.96.2 ``dist/http-server-single-session.js``：名額滿時 initialize 回 429＋-32000
``Session limit reached (${MAX_SESSIONS}). Please wait for existing sessions to expire.``；``DELETE /mcp`` 帶
``Mcp-Session-Id`` 時 ``removeSession(…, 'manual_termination')`` 立刻釋放名額並回 204。映像建置時
``verify_upstream_session_limit_reply()`` 確認這兩件事沒變（變了閘道就認不出「名額已滿」或釋放不了名額）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from n8n_mcp_admin import child_env

APP_DIR = Path(__file__).resolve().parents[1]

LIMIT_BLOCK = """
                    if (!this.canCreateSession()) {
                        res.status(429).json({
                            jsonrpc: '2.0',
                            error: {
                                code: -32000,
                                message: `Session limit reached (${MAX_SESSIONS}). Please wait for existing sessions to expire.`
                            },
                            id: req.body?.id ?? null
                        });
                        return;
                    }
"""
DELETE_BLOCK = """
        app.delete('/mcp', authLimiter, async (req, res) => {
            const mcpSessionId = req.headers['mcp-session-id'];
            if (this.transports[mcpSessionId]) {
                try {
                    await this.removeSession(mcpSessionId, 'manual_termination');
                    res.status(204).send();
                }
"""


def _fake_pkg(tmp_path: Path, *blocks: str) -> str:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "http-server-single-session.js").write_text("const SESSION_CLEANUP_INTERVAL = 5 * 60 * 1000;\n" + "".join(blocks))
    return str(tmp_path)


def test_contract_holds_for_the_reviewed_upstream_shape(tmp_path):
    pkg = _fake_pkg(tmp_path, LIMIT_BLOCK, DELETE_BLOCK)
    assert child_env.upstream_session_limit_contract(pkg) == []
    assert child_env.verify_upstream_session_limit_reply(pkg) == 0


def test_contract_fails_when_upstream_changes_the_reply_or_delete(tmp_path):
    changed_text = _fake_pkg(tmp_path / "a", LIMIT_BLOCK.replace("Session limit reached", "Too many sessions"), DELETE_BLOCK)
    assert child_env.verify_upstream_session_limit_reply(changed_text) == 1
    no_release = _fake_pkg(tmp_path / "b", LIMIT_BLOCK, DELETE_BLOCK.replace("res.status(204)", "res.status(202)"))
    assert len(child_env.upstream_session_limit_contract(no_release)) == 1
    assert child_env.verify_upstream_session_limit_reply(str(tmp_path / "missing")) == 1


def test_real_upstream_package_when_available():
    pkg = os.environ.get("N8N_MCP_PACKAGE_DIR")
    if not pkg or not os.path.isdir(pkg):
        pytest.skip("n8n-mcp 套件不在本機（映像建置時 Dockerfile 會驗證）")
    assert child_env.upstream_session_limit_contract(pkg) == []


def test_dockerfile_verifies_the_session_limit_contract():
    dockerfile = (APP_DIR / "Dockerfile").read_text()
    assert "verify_upstream_session_limit_reply" in dockerfile


def test_reply_prefix_matches_the_upstream_template():
    # child_env.is_session_limit_reply 比對的前綴＝上游模板在 ${MAX_SESSIONS} 之前的文字
    assert "Session limit reached (${MAX_SESSIONS})".startswith(child_env.SESSION_LIMIT_PREFIX)


def test_app_declares_the_gateway_reclaimer():
    from n8n_mcp_admin import main

    reclaimer = main.app.state.session_reclaimer
    assert reclaimer is main.SESSION_RECLAIMER
    assert reclaimer.is_session_limit is child_env.is_session_limit_reply
    assert reclaimer.idle_floor_seconds == 300  # 第二輪審查：60 秒對人在對話中的停頓偏短
    assert reclaimer.lifetime({"mcp_session_timeout": "900"}) == 15 * 60 + child_env.UPSTREAM_SESSION_CLEANUP_SECONDS


def test_defaults_follow_measured_session_memory():
    # 2026-10-26：逾時 3600 → 900 秒、名額 10 → 20（每個 session 約 1.6～3.1 MiB，pod limits 1Gi）
    assert child_env.SESSION_TIMEOUT_DEFAULT_SECONDS == 900
    assert child_env.MAX_SESSIONS_DEFAULT == 20
    assert child_env.derived_env({}) == {"SESSION_TIMEOUT_MINUTES": "15", "N8N_MCP_MAX_SESSIONS": "20"}


def _field(name: str) -> tuple[str, str]:
    src = (APP_DIR / "frontend" / "src" / "appConfig.jsx").read_text()
    line = next(line for line in src.splitlines() if f"name: '{name}'" in line)
    default = re.search(r"default: (\d+)", line).group(1)
    return default, re.search(r"help: '([^']*)'", line).group(1)


def test_admin_page_fields_match_the_gateway_behaviour():
    """DATA-NMCP-003-3：管理頁說明要跟實際行為一致（預設值、5 分鐘清掃週期、GET 串流不算使用中、名額滿回 429 與處置）。"""
    default, help_text = _field("mcp_session_timeout")
    assert default == str(child_env.SESSION_TIMEOUT_DEFAULT_SECONDS) and "預設 900" in help_text
    assert "5 分鐘" in help_text and "閘道" in help_text and "開著 GET 串流不算使用中" in help_text
    default, help_text = _field("mcp_max_sessions")
    assert default == str(child_env.MAX_SESSIONS_DEFAULT) and "預設 20" in help_text
    # 本版自動回收關閉（f-2）：名額滿回 429，直到舊 session 逾時；說明要寫處置，不能再說閘道會先結束 session。
    for words in ("5 分鐘", "429", "不會主動結束", "Session Timeout", "15～20 分鐘", "調高", "Restart", "DELETE",
                  "Claude Code"):
        assert words in help_text, words
    for stale in ("每分鐘最多結束 10 個", "閘道先結束", "404", "60 秒"):
        assert stale not in help_text, stale


def test_connection_api_defaults_come_from_child_env():
    from n8n_mcp_admin.routers.config import ConnectionConfig, ConnectionUpdate

    assert ConnectionConfig(configuration={}).mcp_session_timeout == child_env.SESSION_TIMEOUT_DEFAULT_SECONDS
    assert ConnectionConfig(configuration={}).mcp_max_sessions == child_env.MAX_SESSIONS_DEFAULT
    update = ConnectionUpdate(n8n_api_url="", n8n_api_key="")
    assert (update.mcp_session_timeout, update.mcp_max_sessions) == (900, 20)
