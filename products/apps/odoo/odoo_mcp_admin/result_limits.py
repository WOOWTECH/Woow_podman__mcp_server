"""``search_records``／``read_record`` 的回應大小上限（商用驗收 DATA-OMCP-007-3 延伸）。

## 現象
``search_records(model="ir.attachment", fields=["datas"])`` 把附件的 base64 整包放進回應：3 筆就
17.7 MiB，14 個 JS asset 要讀 125 秒，被 Cloudflare 以 524 切斷；就算送得到，也遠超過任何 LLM 的上下文。
上游 ``read_attachment`` 有 1 MiB 上限（``ODOO_MCP_MAX_ATTACHMENT_BYTES``），這兩支泛用讀取工具沒有。

## 修法
工具成功回傳後量測 ``result``（JSON 序列化後的位元組數）。超過 ``ODOO_MCP_MAX_RESULT_BYTES``
（預設 2 MiB，``0`` 關閉）時改回 ``{"success": false, ...}``——再由 ``tool_errors`` 標成 isError——
訊息列出最大的幾個欄位，指引改用 ``fields`` 排除二進位欄位、縮小 ``limit``，或用 ``read_attachment``
讀檔案內容。不禁止任何欄位：小的二進位欄位（縮圖等）照常回傳。

``submit_async_task`` 的 allowlist 不含 search_records／read_record，所以不把它當成替代方案提示。
必須在 ``tool_errors.install`` 之前呼叫，見 ``launcher.py``。
"""

from __future__ import annotations

import functools
import inspect
import json
import os
from typing import Any

LIMITED_TOOLS = ("search_records", "read_record")
DEFAULT_MAX_RESULT_BYTES = 2 * 1024 * 1024
_TOP_FIELDS = 3


def max_result_bytes() -> int:
    raw = os.environ.get("ODOO_MCP_MAX_RESULT_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_RESULT_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_RESULT_BYTES


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode())


def _largest_fields(payload: Any) -> list[str]:
    rows = payload if isinstance(payload, list) else [payload]
    totals: dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            for name, value in row.items():
                totals[name] = totals.get(name, 0) + _size(value)
    return [name for name, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_FIELDS]]


def _mib(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MiB"


def check_result_size(result: Any) -> Any:
    """成功且 ``result`` 過大的回應 → 可讀的 ``success: false``；其他原樣回傳。"""
    if not isinstance(result, dict) or result.get("success") is not True:
        return result
    cap = max_result_bytes()
    if cap <= 0:
        return result
    size = _size(result.get("result"))
    if size <= cap:
        return result
    largest = _largest_fields(result.get("result"))
    shown = ", ".join(largest) or "-"
    return {
        "success": False,
        "error": (
            f"Result is {_mib(size)}, over the {_mib(cap)} limit (ODOO_MCP_MAX_RESULT_BYTES). "
            f"Largest fields: {shown}. Retry with explicit `fields` that leave out binary fields such as "
            "'datas', a smaller `limit`, or read file content with read_attachment (size-capped). "
            f"(結果 {_mib(size)} 超過 {_mib(cap)} 上限；最大的欄位：{shown}。請用 fields 排除 datas 等"
            "二進位欄位、縮小 limit，或改用 read_attachment 讀檔案內容。)"
        ),
        "result_bytes": size,
        "max_result_bytes": cap,
        "largest_fields": largest,
    }


def _wrap(fn):
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            return check_result_size(await fn(*args, **kwargs))
    else:
        @functools.wraps(fn)
        def run(*args, **kwargs):
            return check_result_size(fn(*args, **kwargs))
    return run


def install(mcp: Any) -> list[str]:
    """包上 ``LIMITED_TOOLS``；回傳被包的工具名（排序）。Idempotent。"""
    wrapped = []
    for tool in mcp._tool_manager.list_tools():
        if tool.name not in LIMITED_TOOLS or getattr(tool.fn, "_woow_result_limit", False):
            continue
        new_fn = _wrap(tool.fn)
        new_fn._woow_result_limit = True
        tool.fn = new_fn
        wrapped.append(tool.name)
    return sorted(wrapped)
