"""MCP SDK 2.x 的 ``MCPServer`` 會遮掉工具例外的文字；把它換回 mcp 1.x 的可讀錯誤（#78）。

mcp 1.x 的 FastMCP 對工具丟出的**任何**例外都回 ``isError=true``＋``Error executing tool <name>: <例外文字>``。
mcp 2.1 起的 ``MCPServer`` 只保留 ``ToolError``（與 ``ResourceError``）的文字；其他例外一律只回
``Error executing tool <name>``，原因只進 server log。我們的子行程有好幾個地方靠「例外文字給 LLM／管理者看」：

- hermes：``errors.py`` 與 ``server.py`` 的後端錯誤（``BACKEND_TIMEOUT``、``Hermes Gateway (...) 不可達``…）、
  ``guards.GuardError``（輸入被安全規則拒絕的原因）都是一般例外；
- opendesign：vendored 的 ``od_mcp_server.py``（byte-faithful，不改檔）直接讓 httpx 例外往上拋；
- odoo：上游工具多半自己回 ``success: false``，但仍有少數路徑直接丟例外。

:func:`keep_error_text` 在工具邊界把「``ToolError``／``ResourceError``／``MCPError`` 以外」的例外改丟成
``ToolError(str(例外))``（原例外留在 ``__cause__``），所以 client 看到的文字與 mcp 1.x 逐字相同：
``Error executing tool <name>: <例外文字>``。``MCPError`` 照舊是 JSON-RPC 錯誤；``asyncio.CancelledError``
等 ``BaseException`` 不攔。只換 ``Tool.fn``（同步的仍是同步、async 的仍是 async），參數驗證、schema、名稱都不變；
冪等。
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.shared.exceptions import MCPError

_MARK = "_woow_keeps_error_text"
_PASS_THROUGH = (ToolError, ResourceError, MCPError)


def _wrap(fn):
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except _PASS_THROUGH:
                raise
            except Exception as exc:
                raise ToolError(str(exc)) from exc
    else:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except _PASS_THROUGH:
                raise
            except Exception as exc:
                raise ToolError(str(exc)) from exc
    setattr(run, _MARK, True)
    return run


def keep_error_text(server: Any) -> list[str]:
    """把 *server*（``mcp.server.mcpserver.MCPServer``）每個工具的例外文字保留給 client；回傳包過的工具名（排序）。"""
    wrapped = []
    for tool in server._tool_manager.list_tools():
        if getattr(tool.fn, _MARK, False):
            continue
        tool.fn = _wrap(tool.fn)
        wrapped.append(tool.name)
    return sorted(wrapped)
