"""工具失敗要讓 MCP 用戶端看得出來（商用驗收 DATA-OMCP-004-4／007-2）。

## 現象
上游 ``odoo-mcp`` 的工具把所有失敗包成 ``{"success": false, "error": "..."}`` 後**正常回傳**，
MCP 的 ``result.isError`` 永遠是 false：依賴 isError 判斷失敗的代理會把失敗當成功；後端 Odoo 連不上
時六支讀取工具全回 HTTP 200／isError=false／success:false。引數錯誤時 ``error`` 字串還直接帶著
XML-RPC Fault 裡 Odoo 的完整 Traceback（含 ``/usr/lib/python3/dist-packages/odoo/...`` 路徑）。

## 修法
工具回傳 ``success is False`` 的 dict 時，改回 ``CallToolResult(isError=True, ...)``：文字內容與
structuredContent 仍是同一份 JSON（舊的解析方式不受影響），只是多了 isError 旗標；``error`` 裡的
Traceback 縮成最後一行例外訊息。成功的回傳、框架層錯誤（unknown tool、引數驗證）不動。

structuredContent 要符合工具在 tools/list 公布的 outputSchema：回傳型別不是具名模型（例如 ``Dict[str, Any]``）的工具，
SDK 把結果包成 ``{"result": …}``（``fn_metadata.wrap_output``），錯誤結果也要照包（#78）。不包的話：mcp 1.x 在 server 端
驗證失敗，client 只拿到 pydantic 驗證錯誤（``1 validation error for execute_methodOutput … Field required``）；mcp 2.x 不驗
錯誤結果、原樣送出，但 TS SDK client 連錯誤結果的 structuredContent 都會驗，不符就讓整個呼叫丟例外。

必須在 ``concurrency.install`` 之前呼叫（先包錯誤旗標、再包工作緒），見 ``launcher.py``。
"""

from __future__ import annotations

import functools
import inspect
import json
import re
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

_TRACEBACK = "Traceback (most recent call last)"
_TRAILING_JUNK = re.compile(r"""(?:['"]>|\s)+$""")  # repr(Fault) 的收尾 `'>`，只清這個，不吃訊息本身的引號


def scrub_error_text(text: str) -> str:
    """把夾帶 Odoo Traceback 的錯誤字串縮成最後一行例外訊息；其他字串原樣回傳。

    XML-RPC ``Fault`` 的 ``str()`` 是 ``<Fault 1: 'Traceback…\\n  File "…"\\nodoo.exceptions.X: msg\\n'>``，
    換行是字面的 ``\\n`` 兩個字元，所以兩種換行都要處理。
    """
    if not isinstance(text, str) or _TRACEBACK not in text:
        return text
    cleaned = (_TRAILING_JUNK.sub("", ln.strip()) for ln in text.replace("\\n", "\n").splitlines())
    lines = [ln for ln in cleaned if ln]  # repr 的收尾 `'>` 單獨成行時，清掉後是空字串，要略過
    last = lines[-1].replace("\\'", "'") if lines else ""  # repr 會把訊息裡的單引號跳脫成 \'
    return f"Odoo server error: {last}" if last else "Odoo server error"


def as_error_result(result: Any, *, wrap: bool = False) -> Any:
    """``success is False`` 的 dict → ``CallToolResult(isError=True)``；其他原樣回傳。

    *wrap*：工具的 outputSchema 是 SDK 包裝的 ``{"result": …}`` 時為真，structuredContent 也包一層（見模組說明）；
    文字內容一律是同一份 JSON（不包）。
    """
    # search_holidays / search_employee return typed models, unlike most tools.
    # Preserve successful models for the SDK's normal schema conversion.
    if isinstance(result, BaseModel) and getattr(result, "success", None) is False:
        result = result.model_dump(mode="json")
    if not isinstance(result, dict) or result.get("success") is not False:
        return result
    cleaned = dict(result)
    if isinstance(cleaned.get("error"), str):
        cleaned["error"] = scrub_error_text(cleaned["error"])
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps(cleaned, indent=2, ensure_ascii=False, default=str))],
        structuredContent={"result": cleaned} if wrap else cleaned,
    )


def _wrap(fn, wrap: bool):
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            return as_error_result(await fn(*args, **kwargs), wrap=wrap)
    else:
        @functools.wraps(fn)
        def run(*args, **kwargs):
            return as_error_result(fn(*args, **kwargs), wrap=wrap)
    return run


def install(mcp: Any) -> list[str]:
    """把 ``mcp`` 的每個工具包上錯誤旗標；回傳被包的工具名（排序）。Idempotent。"""
    wrapped = []
    for tool in mcp._tool_manager.list_tools():
        if getattr(tool.fn, "_woow_error_flag", False):
            continue
        new_fn = _wrap(tool.fn, bool(getattr(tool.fn_metadata, "wrap_output", False)))
        new_fn._woow_error_flag = True
        tool.fn = new_fn
        wrapped.append(tool.name)
    return sorted(wrapped)
