"""子行程生命週期／身分拒絕 log 也要進容器 stdout（kubectl logs），而且只一次、遮罩、不含子行程自己的輸出。

mcp_admin_core.process 原本只進管理頁 Logs 的 ring buffer（pod 重建就消失）。全部用合成值與臨時 loopback port。
"""
import asyncio
import logging
import sys

import pytest

from mcp_admin_core import child_identity
from mcp_admin_core.routers import logs
from test_child_identity_live import (INIT, LINUX, SLEEPER, free_port, gateway, last_error, peers, post,  # noqa: F401
                                      stop, wait_until)

FAKE = "synthetic-stdout-log-token-0123456789"
LOGGER = "mcp_admin_core.process"


@pytest.fixture
def ring():
    """裝上 Logs 頁 ring buffer 的 process handler 與 admin handler（app 啟動時的真實狀態），結束後還原。"""
    proc_logger = logging.getLogger(LOGGER)
    parent = logging.getLogger("mcp_admin_core")
    before = (list(proc_logger.handlers), list(parent.handlers), proc_logger.level, parent.level)
    with logs._buffer_lock:
        logs._log_buffer.clear()
    logs.install_log_capture()
    logs._install_admin_log_capture(("mcp_admin_core",))
    yield
    proc_logger.handlers[:] = before[0]
    parent.handlers[:] = before[1]
    proc_logger.setLevel(before[2])
    parent.setLevel(before[3])


def _ring_lines(needle: str) -> list[str]:
    """Logs 頁緩衝區內容（來源:訊息）。每筆 record 只存一次（2026-11-13 P3-e，見 test_logs_profile_buffers.py）：生命週期／
    身分拒絕的來源是 logger 名（mcp_admin_core.process），子行程自己的輸出來源是 mcp-server、去掉前綴。"""
    with logs._buffer_lock:
        return [f"{e['source']}:{e['message']}" for e in logs._log_buffer if needle in e["message"]]


def _sources(lines: list[str]) -> list[str]:
    return sorted(line.split(":", 1)[0] for line in lines)


def _stdout_lines(capsys, needle: str) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if needle in line]


def test_process_logger_has_one_stdout_handler_at_info_and_ring_buffer_unchanged(ring):
    from mcp_admin_core.owner_defaults import _StdoutHandler

    proc_logger = logging.getLogger(LOGGER)
    stdout_handlers = [h for h in proc_logger.handlers if isinstance(h, _StdoutHandler)]
    assert len(stdout_handlers) == 1 and stdout_handlers[0].level == logging.INFO
    assert any(isinstance(h, logs._BufferLogHandler) for h in proc_logger.handlers)  # ring buffer 照舊


@pytest.mark.parametrize("message", [
    "Refusing to start the MCP child on 127.0.0.1:9 (child_port_in_use): another socket is bound",
    "MCP child (pid=7) refused (child_peer_mismatch): the listener is not owned by the child",
    "MCP server auto-restart scheduled in 5s (attempt 1)",
    "MCP server started (pid=7)",
])
def test_lifecycle_message_reaches_stdout_once_and_ring_buffer_once(ring, capsys, message):
    logging.getLogger(LOGGER).warning("%s", message) if "auto-restart" in message else logging.getLogger(LOGGER).info("%s", message)
    assert len(_stdout_lines(capsys, message)) == 1
    assert _sources(_ring_lines(message)) == [LOGGER]  # 只存一次（以前 mcp-server 與 logger 名各一筆）


def test_stdout_line_is_redacted_for_tokens_and_private_paths(ring, capsys):
    logging.getLogger(LOGGER).error("child refused on /private_%s/mcp and /x?token=%s", FAKE, FAKE)
    out = capsys.readouterr().out
    assert "refused" in out and "***" in out
    assert FAKE not in out
    assert not any(FAKE in line for line in _ring_lines("refused"))


def test_stdout_is_redacted_even_without_the_record_factory(capsys):
    """handler 自己的 filter 是第二道：record factory 沒裝（或被換掉）時 stdout 仍不出現 token。"""
    record = logging.LogRecord(LOGGER, logging.ERROR, __file__, 1, "path /private_%s/mcp", (FAKE,), None)
    record.msg, record.args = "path /private_" + FAKE + "/mcp", ()  # 繞過 factory：模擬沒遮過的 record
    for handler in logging.getLogger(LOGGER).handlers:
        if type(handler).__name__ == "_StdoutHandler":
            handler.handle(record)
    out = capsys.readouterr().out
    assert "path" in out and FAKE not in out


def test_child_own_output_stays_out_of_stdout_but_in_ring_buffer(ring, capsys):
    logging.getLogger(LOGGER).info("[mcp-server] %s", "backend said: synthetic-backend-detail", extra={"mcp_child_output": True})
    assert _stdout_lines(capsys, "synthetic-backend-detail") == []
    assert _sources(_ring_lines("synthetic-backend-detail")) == ["mcp-server"]  # 只存一次（以前兩個來源各一筆）


def test_child_output_containing_a_token_is_not_on_stdout_either(ring, capsys):
    """含憑證的子行程輸出會被遮罩改寫成已渲染字串：filter 不能靠 msg 模板。"""
    logging.getLogger(LOGGER).info("[mcp-server] %s", f"GET /private_{FAKE}/mcp failed synthetic-backend-detail", extra={"mcp_child_output": True})
    out = capsys.readouterr().out
    assert "synthetic-backend-detail" not in out and FAKE not in out


def test_debug_is_not_written_to_stdout(ring, capsys):
    logging.getLogger(LOGGER).debug("synthetic-debug-line")
    assert _stdout_lines(capsys, "synthetic-debug-line") == []


@pytest.mark.skipif(not LINUX, reason="needs /proc to see who owns the listener")
async def test_port_in_use_and_auto_restart_scheduled_reach_stdout_once(make_app, monkeypatch, peers, ring, capsys):  # noqa: F811
    port = free_port()
    await peers.rival(port)
    app, pm = gateway(make_app, monkeypatch, port, SLEEPER)
    try:
        assert not await pm.start()
        out = capsys.readouterr().out
    finally:
        await stop(pm)
    refusing = [line for line in out.splitlines() if "Refusing to start the MCP child" in line]
    scheduled = [line for line in out.splitlines() if "auto-restart scheduled" in line]
    assert len(refusing) == 1 and "child_port_in_use" in refusing[0]
    assert len(scheduled) == 1
    assert _sources(_ring_lines("Refusing to start the MCP child")) == [LOGGER]


async def test_port_conflict_reaches_stdout_once(make_app, monkeypatch, ring, capsys):
    port = free_port()
    monkeypatch.setattr(child_identity, "admin_listener_ports", lambda: {port})
    app, pm = gateway(make_app, monkeypatch, port, SLEEPER)
    try:
        assert not await pm.start()
        out = capsys.readouterr().out
    finally:
        await stop(pm)
    lines = [line for line in out.splitlines() if "child_port_conflict" in line]
    assert len(lines) == 1 and "ERROR" in lines[0]


@pytest.mark.skipif(not LINUX, reason="needs /proc to see who owns the listener")
async def test_peer_mismatch_reaches_stdout_once(make_app, monkeypatch, peers, ring, capsys):  # noqa: F811
    port = free_port()
    app, pm = gateway(make_app, monkeypatch, port, SLEEPER)
    try:
        assert await pm.start()
        probe, child = pm._configuration_task, pm._process
        await peers.rival(port)
        await asyncio.wait_for(probe, 15)
        assert await wait_until(lambda: child.returncode is not None)
        out = capsys.readouterr().out
    finally:
        await stop(pm)
    lines = [line for line in out.splitlines() if "MCP child (pid=" in line and "refused (child_peer_mismatch)" in line]
    assert len(lines) == 1, out


def test_connection_apply_restore_failure_reaches_stdout_once(capsys):
    logging.getLogger("mcp_admin_core.connection_apply").warning("Could not restore MCP child after failed connection save")
    assert len(_stdout_lines(capsys, "Could not restore MCP child")) == 1


# ---- F8：子行程輸出的排除改看結構標記，不看訊息文字 ----------------------------------------------------------------

class _FakeStdout:
    def __init__(self, lines):
        self._lines = [line.encode() + b"\n" for line in lines]

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.returncode = 0

    async def wait(self):
        return 0


async def _drain(lines):
    """跑真的 ``_drain_logs``（子行程已被換掉的路徑：讀完輸出就返回，不碰重啟邏輯）。"""
    from collections import deque
    from types import SimpleNamespace

    from mcp_admin_core.process import McpProcessManager

    fake = SimpleNamespace(_last_output=deque(maxlen=20), _process=object())
    await McpProcessManager._drain_logs(fake, _FakeProc(lines))
    return list(fake._last_output)


CHILD_LINES = {
    "token": f"GET /private_{FAKE}/mcp failed synthetic-backend-detail",
    "invalid_escape": "GET /x?token=synthetic-backend-detail%zz",
    "plain": "backend said: synthetic-backend-detail",
}


@pytest.mark.parametrize("kind", sorted(CHILD_LINES))
async def test_child_output_never_reaches_stdout_but_always_reaches_logs_page(ring, capsys, kind):
    from mcp_admin_core.log_redaction import AMBIGUOUS

    kept = await _drain([CHILD_LINES[kind]])
    out = capsys.readouterr().out
    assert kept == [CHILD_LINES[kind]]  # status 的最後輸出行不變
    assert out == "" and FAKE not in out and AMBIGUOUS not in out  # 一個字都不進 stdout（含佔位行）
    with logs._buffer_lock:
        entries = [(e["source"], e["message"]) for e in logs._log_buffer]
    shown = {"token": f"GET /private_***/mcp failed synthetic-backend-detail",
             "invalid_escape": AMBIGUOUS,
             "plain": CHILD_LINES["plain"]}[kind]
    # 只存一次：來源 mcp-server、去前綴（ec82641～e0a80d6 另有往上傳的 admin handler 存一份帶前綴的，2026-11-13 P3-e 移除）
    assert entries == [("mcp-server", shown)]


async def test_masked_child_line_and_lifecycle_line_do_not_interfere(ring, capsys):
    await _drain([CHILD_LINES["invalid_escape"]])
    logging.getLogger(LOGGER).info("MCP server started (pid=7)")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and "MCP server started (pid=7)" in lines[0]


def test_stdout_exclusion_does_not_depend_on_the_message_text(ring, capsys):
    """標記才是依據：訊息長得像子行程輸出但沒有標記，照常進 stdout；有標記的，訊息是什麼都不進。"""
    from mcp_admin_core.process import CHILD_OUTPUT_ATTR

    logging.getLogger(LOGGER).info("[mcp-server] looks-like-child-but-lifecycle")
    logging.getLogger(LOGGER).info("[redacted: ambiguous URL encoding]", extra={CHILD_OUTPUT_ATTR: True})
    out = capsys.readouterr().out
    assert "looks-like-child-but-lifecycle" in out and "ambiguous" not in out
