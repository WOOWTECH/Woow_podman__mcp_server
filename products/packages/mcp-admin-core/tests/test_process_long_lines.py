"""子行程輸出一行超過 64 KiB：``_drain_logs`` 不能因此停止讀 pipe（#141 作者本機重現的 P3）。

``asyncio.StreamReader`` 預設只收 64 KiB 一行；``async for line in proc.stdout`` 遇到更長的行會拋 ``ValueError``
（``LimitOverrunError``），``_drain_logs`` 的 ``except Exception: pass`` 把它吞掉並離開迴圈。之後沒有人讀 pipe：子行程
的下一次寫入在 pipe 滿了（Linux 預設 64 KiB）之後就卡住，管理程序卻因為 ``await proc.wait()`` 以為一切正常。

現在 ``_drain_logs`` 自己切行、每行最多留 :data:`MAX_CHILD_LINE_BYTES`（與以前一整行能收的上限相同）：更長的部分丟掉、
不另外成行（免得 token 被切在兩行裡、第二行沒有 ``/private_`` 前綴而漏遮），行尾標上 ``…[truncated N bytes]``；
仍然經 log_redaction 遮罩，只進 Logs 頁（子行程輸出本來就不進容器 stdout）。全部用合成子行程與合成值。
"""
from __future__ import annotations

import asyncio
import collections
import logging
import sys
from types import SimpleNamespace

import pytest

from mcp_admin_core import log_redaction
from mcp_admin_core.process import CHILD_OUTPUT_ATTR, MAX_CHILD_LINE_BYTES, McpProcessManager
from mcp_admin_core.routers import logs

FAKE = "synthetic-longline-token-0123456789abcdef"  # 合成值，不是真的 token
LIMIT = MAX_CHILD_LINE_BYTES


@pytest.fixture
def ring():
    """裝上遮罩與 Logs 頁 ring buffer（``create_app()`` 啟動時的真實狀態），結束後還原 handler。

    遮罩（record factory）是全域、冪等的安裝：不能靠其他測試先裝好——單獨跑這個檔案時就沒有人裝，token 不會被遮。"""
    log_redaction.install()
    proc_logger = logging.getLogger("mcp_admin_core.process")
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


def _page_messages() -> list[str]:
    """Logs 頁上子行程輸出的每一筆（已遮罩、去掉前綴）。"""
    with logs._buffer_lock:
        return [entry["message"] for entry in logs._log_buffer if entry["source"] == "mcp-server"]


def _child_code(body: str) -> str:
    return (
        "import sys\n"
        "out = sys.stdout.buffer\n"
        f"FAKE = {FAKE.encode()!r}\n"
        f"{body}\n"
        "out.flush()\n"
    )


async def _run(body: str, *, timeout: float = 30.0):
    """啟動合成子行程（與 McpProcessManager 一樣 stdout=PIPE、stderr 併進 stdout），跑真的 ``_drain_logs`` 到子行程結束。"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _child_code(body),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    fake = SimpleNamespace(_last_output=collections.deque(maxlen=20), _process=object())
    try:
        # 子行程已被換掉的路徑（_process 不是 proc）：讀完輸出、等到結束就返回，不碰重啟邏輯
        await asyncio.wait_for(McpProcessManager._drain_logs(fake, proc), timeout)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode, list(fake._last_output)


def _marker(dropped: int) -> str:
    return f" …[truncated {dropped} bytes]"


async def test_child_writing_huge_lines_does_not_stall_and_later_lines_are_still_read(ring, capsys):
    """以前：迴圈在第一個超長行結束，子行程在 pipe 滿了之後卡住、_drain_logs 永遠等不到它結束（這裡的 wait_for 逾時）。"""
    code, kept = await _run(
        "out.write(b'before-line\\n')\n"
        "out.write(b'x' * 300_000 + b'\\n')\n"
        "out.write(b'middle-line\\n')\n"
        "out.write(b'y' * 100_000 + b'\\n')\n"
        "out.write(b'after-line\\n')\n"
        "out.write(b'z' * 3_000_000)\n")  # 最後一行 3 MB，沒有換行就結束
    assert code == 0
    page = _page_messages()
    assert [page[0], page[2], page[4]] == ["before-line", "middle-line", "after-line"]
    assert len(page) == 6
    for index, size, char in ((1, 300_000, "x"), (3, 100_000, "y"), (5, 3_000_000, "z")):
        assert page[index] == char * LIMIT + _marker(size - LIMIT)
    assert kept == page  # status 的最後輸出行也是同樣的行（沒有 token，遮罩前後相同）
    assert capsys.readouterr().out == ""  # 子行程輸出一個字都不進容器 stdout


async def test_lines_up_to_the_limit_are_kept_whole_exactly_as_before(ring):
    """以前整行收得下的（≤ 64 KiB）一個位元組都不動、沒有標記。"""
    code, _ = await _run(
        f"out.write(b'a' * {LIMIT} + b'\\n')\n"
        f"out.write(b'b' * {LIMIT - 1} + b'\\r\\n')\n"
        "out.write(b'tail-without-newline')")
    assert code == 0
    assert _page_messages() == ["a" * LIMIT, "b" * (LIMIT - 1), "tail-without-newline"]


async def test_one_byte_over_the_limit_is_truncated_and_marked(ring):
    code, _ = await _run(f"out.write(b'c' * {LIMIT + 1} + b'\\n')\nout.write(b'next\\n')")
    assert code == 0
    assert _page_messages() == ["c" * LIMIT + _marker(1), "next"]


async def test_token_straddling_the_cut_is_never_logged_in_pieces(ring):
    """token 剛好橫跨截斷點：留下的前半經遮罩變成 ``***``；被丟掉的後半不另外成行（否則第二段沒有 /private_ 前綴就漏遮）。"""
    lead = LIMIT - 20  # 前綴到截斷點還剩 20 個位元組：「/private_」加 token 的前 11 個字元在留下的部分、其餘在被丟掉的部分
    straddling = lead + len("/private_") + len(FAKE) + len("/mcp ") + 500_000
    code, kept = await _run(
        f"out.write(b'p' * {lead} + b'/private_' + FAKE + b'/mcp ' + b'q' * 500_000 + b'\\n')\n"
        # 另一行：token 整個在留下的部分；再一行：token 整個在被丟掉的部分
        "out.write(b'early /private_' + FAKE + b'/mcp ' + b'r' * 200_000 + b'\\n')\n"
        "out.write(b's' * 200_000 + b' /private_' + FAKE + b'/mcp\\n')\n"
        "out.write(b'done\\n')")
    assert code == 0
    page = _page_messages()
    assert len(page) == 4 and page[3] == "done"
    everything = "\n".join(page)
    for start in range(len(FAKE) - 7):  # 任何 8 個字元以上的 token 片段都不可出現在 Logs 頁
        assert FAKE[start:start + 8] not in everything
    assert page[0] == "p" * lead + "/private_***" + _marker(straddling - LIMIT)
    assert page[1].startswith("early /private_***/mcp rrrr") and page[1].endswith(_marker(len("early /private_") + len(FAKE) + 5 + 200_000 - LIMIT))
    assert page[2] == "s" * LIMIT + _marker(200_000 + len(" /private_") + len(FAKE) + len("/mcp") - LIMIT)


async def test_invalid_utf8_and_a_cut_inside_a_multibyte_character_do_not_break_the_loop(ring):
    code, _ = await _run(
        "out.write('\\u6211'.encode() * 40_000 + b'\\n')\n"  # 120,000 位元組，切點落在字元中間
        "out.write(b'\\xff\\xfe bad utf8\\n')\n"
        "out.write(b'ok\\n')")
    page = _page_messages()
    assert code == 0 and len(page) == 3 and page[2] == "ok"
    assert page[0].count("我") == LIMIT // 3 and page[0].endswith(_marker(120_000 - LIMIT))
    assert "bad utf8" in page[1]


async def test_the_status_last_output_keeps_only_the_truncated_text():
    """exit 原因（status 的 last_output）也是截斷後的行，不會因為一行過長而佔用大量記憶體。"""
    code, kept = await _run("out.write(b'e' * 1_000_000 + b'\\n')\nout.write(b'done\\n')")
    assert code == 0 and kept[-1] == "done"
    assert all(len(line) <= LIMIT + 64 for line in kept)


class _LineIterable:
    """只能逐行迭代的輸出（既有測試的替身，不是 StreamReader）：照舊一行一行讀。"""

    def __init__(self, lines):
        self._lines = lines

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line


class _Proc:
    def __init__(self, lines):
        self.stdout = _LineIterable(lines)
        self.returncode = 0

    async def wait(self):
        return 0


async def test_line_iterables_are_still_supported_and_oversized_lines_are_cut_there_too(ring):
    fake = SimpleNamespace(_last_output=collections.deque(maxlen=20), _process=object())
    giant = b"g" * (LIMIT + 10) + b"\n"
    await McpProcessManager._drain_logs(fake, _Proc([b"one\n", giant, b"two\r\n", b"\n", b"   \n", b"three"]))
    page = _page_messages()
    assert page == ["one", "g" * LIMIT + _marker(10), "two", "three"]
    assert list(fake._last_output) == page  # 空行與只有空白的行照舊略過


def test_constants():
    assert CHILD_OUTPUT_ATTR == "mcp_child_output"
    assert LIMIT == 64 * 1024  # 與 asyncio.StreamReader 預設 limit 相同：以前整行收得下的上限
