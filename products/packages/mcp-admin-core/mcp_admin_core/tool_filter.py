"""在 MCP reverse proxy 層強制執行 Tool Manager 的 per-tool 停用（#1384）。

`config.json` 的 ``tools.disabled`` 過去只有 k8s app（launcher 轉 TOML）會消費，其餘 app 的子行程
（FastMCP／supergateway／上游 binary）都沒有過濾參數，導致後台停用工具完全不生效。這裡改在所有 app
共用的 proxy 解析 JSON-RPC：

- 請求：``tools/call`` 指名被停用的工具 → 不轉發給子行程。Streamable HTTP（``/mcp``）直接回
  ``isError: true`` 的工具結果（見下）；legacy SSE（``/messages/``）回應只能經 GET 串流送回，改寫工具名讓
  子行程回「unknown tool」錯誤（client 不會卡住等不到回應）。
- 拒絕的回法（2026-10-26 起）：工具結果 ``{"content": [text], "isError": true}``，文字照舊說明是管理員停用。
  MCP 2025-11-25 把政策／業務規則的拒絕歸為 Tool Execution Error（client **SHOULD** 交給模型；JSON-RPC 的
  Protocol Error 只是 MAY），子行程對同一種拒絕也是這樣回（n8n-mcp 的 TOOL_DISABLED／OPERATION_DISABLED、
  MCP SDK 2.x 的 Unknown tool、litellm 的 ToolError），batch 混合與 legacy SSE 交給子行程的那條路也是 isError——
  同一個拒絕只有一種形狀（比照 AI-HMCP-003-3、DATA-NMCP-005-2 的 isError 原則）。過去回 JSON-RPC -32602。
- 回應：``tools/list`` 結果（``result.tools``）移除被停用的工具；JSON 與 SSE 兩種回應格式都處理。

停用清單每次請求／每個 tools/list 事件即時讀 config store，所以 Tool Manager 儲存後立即生效，
不必重啟子行程、也不會中斷現有 session。
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

DISABLED_NAME_SUFFIX = " (disabled by administrator)"

_EVENT_BOUNDARY = re.compile(rb"\r\n\r\n|\n\n|\r\r")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def prefix_endpoint(data: str, prefix: str) -> str:
    """把 legacy SSE ``endpoint`` 事件的 data 改成 proxy 對外路徑（補 ``/private_<token>``）。

    子行程只知道自己在 loopback 根路徑，回的是 root-relative（``/messages/?session_id=…``）或
    指回 loopback 的絕對網址；client 用 ``urljoin(<sse url>, data)`` 算出的 POST 位址會掉前綴
    （或因 origin 不符被 SDK 拒絕）。相對路徑、已帶前綴、指向其他主機的網址都原樣保留。
    """
    target = data
    if data.startswith(("http://", "https://")):
        parts = urlsplit(data)
        if (parts.hostname or "") not in _LOOPBACK_HOSTS:
            return data
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    elif not data.startswith("/") or data.startswith("//"):
        return data
    if target == prefix or target.startswith(prefix + "/") or target.startswith(prefix + "?"):
        return target
    return prefix + target


def normalize_disabled(value: Any) -> frozenset[str]:
    """``tools.disabled`` 可能是 list 或逗號分隔字串（與 routers/tools.py 讀法一致）。"""
    if isinstance(value, list):
        return frozenset(str(v) for v in value if isinstance(v, str) and v)
    if isinstance(value, str) and value.strip():
        return frozenset(t.strip() for t in value.split(",") if t.strip())
    return frozenset()


async def read_disabled_tools(store) -> frozenset[str]:
    try:
        tools_cfg = await store.get("tools", {})
    except Exception:
        return frozenset()
    if not isinstance(tools_cfg, dict):
        return frozenset()
    return normalize_disabled(tools_cfg.get("disabled", []))


def parse_jsonrpc(body: bytes) -> Any:
    """回傳解析後的 JSON-RPC 訊息（dict 或 batch list）；不是 JSON 則回 None。"""
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, (dict, list)) else None


def messages(data: Any) -> list[dict]:
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    return []


def requests_method(data: Any, method: str) -> bool:
    return any(m.get("method") == method for m in messages(data))


def _called_tool(msg: dict) -> str | None:
    if msg.get("method") != "tools/call":
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return name if isinstance(name, str) else None


def disabled_calls(data: Any, disabled: frozenset[str]) -> list[dict]:
    """列出指名被停用工具的 tools/call 請求。"""
    if not disabled:
        return []
    return [m for m in messages(data) if _called_tool(m) in disabled]


def refusal_result(msg: dict, text: str) -> dict:
    """閘道依政策拒絕的 ``tools/call`` 的回應：``isError: true`` 的工具結果（見模組說明），不帶 structuredContent
    （MCP Python／TS SDK 的 client 對 isError 結果都不驗 outputSchema）。"""
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def disabled_error(msg: dict) -> dict:
    """被 Tool Manager 停用的工具：回 isError 的工具結果（名稱沿用；過去是 JSON-RPC -32602）。"""
    name = _called_tool(msg)
    return refusal_result(msg, f"Tool '{name}' is disabled by the administrator (此工具已被管理員停用)")


def rewrite_disabled_calls(data: Any, disabled: frozenset[str]) -> bytes:
    """把被停用工具的 tools/call 改名，讓子行程回 unknown tool（用於無法直接回應的路徑）。"""
    for m in disabled_calls(data, disabled):
        m["params"]["name"] = m["params"]["name"] + DISABLED_NAME_SUFFIX
    return json.dumps(data, ensure_ascii=False).encode()


def filter_tools_result(data: Any, disabled: frozenset[str]) -> bool:
    """就地移除 ``result.tools`` 中被停用的工具；有改動回 True。"""
    changed = False
    for m in messages(data):
        result = m.get("result")
        if not isinstance(result, dict):
            continue
        tools = result.get("tools")
        if not isinstance(tools, list):
            continue
        kept = [t for t in tools if not (isinstance(t, dict) and t.get("name") in disabled)]
        if len(kept) != len(tools):
            result["tools"] = kept
            changed = True
    return changed


def filter_json_body(body: bytes, disabled: frozenset[str]) -> bytes:
    data = parse_jsonrpc(body)
    if data is None or not filter_tools_result(data, disabled):
        return body
    return json.dumps(data, ensure_ascii=False).encode()


class SseToolsFilter:
    """逐事件改寫 SSE 串流：tools/list 結果移除停用工具，legacy ``endpoint`` 事件補 proxy 前綴。

    只有完整事件（以空行結尾）才會送出；不含 ``"tools"`` 的事件原樣通過、不解析 JSON。
    停用清單在遇到 tools 事件時才讀（長命 GET /sse 串流也能吃到後來的設定變更）。
    ``endpoint_prefix``（例如 ``/private_<token>``）給定時，``event: endpoint`` 的 data 經
    :func:`prefix_endpoint` 改寫，讓 client 的 POST 回傳通道也走 proxy。
    """

    def __init__(
        self,
        get_disabled: Callable[[], Awaitable[frozenset[str]]],
        *,
        endpoint_prefix: str | None = None,
    ) -> None:
        self._get_disabled = get_disabled
        self._endpoint_prefix = endpoint_prefix
        self._buf = b""

    async def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        out: list[bytes] = []
        while True:
            m = _EVENT_BOUNDARY.search(self._buf)
            if not m:
                break
            event, sep = self._buf[: m.start()], m.group(0)
            self._buf = self._buf[m.end():]
            out.append(await self._rewrite(event, sep) + sep)
        return b"".join(out)

    def flush(self) -> bytes:
        rest, self._buf = self._buf, b""
        return rest

    def _rewrite_endpoint(self, lines: list[bytes], eol: bytes) -> bytes | None:
        """``event: endpoint`` 事件 → 改寫後的事件；其他事件回 None。"""
        if not any(ln[6:].strip() == b"endpoint" for ln in lines if ln.startswith(b"event:")):
            return None
        rebuilt: list[bytes] = []
        for ln in lines:
            if ln.startswith(b"data:"):
                data = ln[5:].lstrip(b" ").decode("utf-8", "replace")
                new = prefix_endpoint(data, self._endpoint_prefix)
                if new != data:
                    ln = b"data: " + new.encode()
            rebuilt.append(ln)
        return eol.join(rebuilt)

    async def _rewrite(self, event: bytes, sep: bytes) -> bytes:
        eol = sep[: len(sep) // 2]
        if self._endpoint_prefix and b"endpoint" in event:
            rewritten = self._rewrite_endpoint(event.split(eol), eol)
            if rewritten is not None:
                return rewritten
        if b'"tools"' not in event:
            return event
        lines = event.split(eol)
        data_lines = [ln for ln in lines if ln.startswith(b"data:")]
        if not data_lines:
            return event
        payload = b"\n".join(ln[5:].lstrip(b" ") for ln in data_lines)
        data = parse_jsonrpc(payload)
        if data is None:
            return event
        disabled = await self._get_disabled()
        if not disabled or not filter_tools_result(data, disabled):
            return event
        new_data = b"data: " + json.dumps(data, ensure_ascii=False).encode()
        rebuilt: list[bytes] = []
        placed = False
        for ln in lines:
            if ln.startswith(b"data:"):
                if not placed:
                    rebuilt.append(new_data)
                    placed = True
                continue
            rebuilt.append(ln)
        return eol.join(rebuilt)
