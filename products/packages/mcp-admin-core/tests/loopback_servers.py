"""測試用的 loopback 伺服器：背景 thread 跑真的 uvicorn，以及只為閘道測試寫的假子行程。

假子行程不是 MCP 實作，只模擬閘道看得到的形狀：``GET /mcp`` 開一條不會自己結束的 SSE 串流（像 Claude Code 連上後
一直開著的那條），``POST /mcp`` 立刻回 JSON-RPC；``method`` 是 ``hold`` 的 POST 一直不回（佔住一條連線），直到用戶端
斷線或伺服器關閉。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ThreadedUvicorn:
    """``with ThreadedUvicorn(app) as server:`` 在背景 thread 跑 uvicorn（自己的事件迴圈），``server.port`` 是 loopback port。"""

    def __init__(self, app, port: int | None = None, *, lifespan: str = "off") -> None:
        # port 省略時綁 0、啟動後再讀實際的 port（同一台機器上其他測試也在開 port，先找空 port 再綁會撞到）。
        self.port = port or 0
        # log_config／log_level 都不設：不改全域 logging 設定，uvicorn 的紀錄照常往上傳（測試自己掛 handler 收）。
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level=None, lifespan=lifespan, log_config=None,
            timeout_graceful_shutdown=2,
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "ThreadedUvicorn":
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("test server did not start")
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


class FakeChildState:
    def __init__(self) -> None:
        self.open_streams = 0
        self.held = 0
        self.posts = 0
        #: 設定後，佔住的 POST 全部回應（閘道等子行程回應標頭時不會因為用戶端斷線就取消，要從子行程這邊放開）。
        self.release_holds = threading.Event()


def fake_child_app() -> tuple[Starlette, FakeChildState]:
    state = FakeChildState()

    async def mcp(request: Request):
        if request.method == "GET":
            async def events():
                state.open_streams += 1
                try:
                    yield b": stream open\n\n"
                    while True:
                        await asyncio.sleep(3600)
                finally:
                    state.open_streams -= 1
            return StreamingResponse(events(), media_type="text/event-stream")
        state.posts += 1
        message = json.loads(await request.body() or b"{}")
        if message.get("method") == "hold":
            state.held += 1
            try:
                deadline = time.monotonic() + 60
                while (not state.release_holds.is_set() and time.monotonic() < deadline
                       and not await request.is_disconnected()):
                    await asyncio.sleep(0.02)
            finally:
                state.held -= 1
        return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": []}})

    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])]), state


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(interval)


_MCP_CHILD = Path(__file__).with_name("_streamable_slow_server.py")


@contextlib.contextmanager
def mcp_child_process():
    """真的 MCP 子行程（MCP SDK ``MCPServer``，與正式的 odoo 子行程同一種 Streamable HTTP 實作），另一個 process。

    yield ``(port, Popen)``；測試可以對它送 SIGSTOP／SIGKILL（模擬子行程當掉）。
    """
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(_MCP_CHILD), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("test MCP child did not start")
                time.sleep(0.05)
        yield port, proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


class RecordHandler:
    """掛在 logger 上收集 record（uvicorn 的 logger 可能不往 root 傳，caplog 不一定收得到）。"""

    def __init__(self, *names: str) -> None:
        import logging

        self.records: list = []
        self._loggers = [logging.getLogger(name) for name in names]
        handler = logging.Handler(level=logging.DEBUG)
        handler.emit = self.records.append  # type: ignore[method-assign]
        self._handler = handler

    def __enter__(self) -> "RecordHandler":
        for logger in self._loggers:
            logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc) -> None:
        for logger in self._loggers:
            logger.removeHandler(self._handler)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]
