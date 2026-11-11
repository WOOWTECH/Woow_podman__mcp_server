"""loopback MCP JSON-RPC helper（Test 路徑一的協定層）。

不引入 mcp SDK：用 httpx 對子行程（原生 launcher ``opendesign-mcp-server`` 的 Streamable HTTP
橋出的 ``/mcp`` endpoint）完成三往返：

1. ``initialize``（protocolVersion 2025-03-26）→ 自回應 header 擷取 ``Mcp-Session-Id``。
2. ``notifications/initialized``（帶 session header 的通知，無回應內容需求）。
3. ``tools/call``（帶 session header）→ 解析 result。

上游同埠同時支援 JSON 與 SSE-framed 回應（Accept 兩者皆帶）；本 helper 兩種都解。

**為何要 raw JSON-RPC 而非可達性探測**：Test 路徑一忠實對映子行程實際對話的路徑——
讓子行程自己走完整 MCP 協定去 ``tools/call health``，回得了 daemon
的 health 才算「橋活著＋整鏈通」。可達性探測只證 daemon ``/api/health`` 在線，證不了
「子行程的 MCP HTTP 是否真的通」（daemon 活但子行程死＝假 GREEN）。

形比照 apps/k8s/k8s_mcp_admin/mcp_client.py，但 opendesign 的 ``health`` 工具無 RBAC
維度，故不做 forbidden/RBAC 分類（isError 一律歸 ``tool_error``）。

傳輸層例外刻意分成三類，讓呼叫端分層誠實：
- ``McpTimeout``：HTTP 有連上但逾時（橋活著、daemon 未在時限內回應）。
- ``McpUnavailable``：連不上 loopback（子行程剛 restart、HTTP listener
  尚未就緒；core ``is_running`` 只看 returncode，node 橋有啟動窗口）。
- ``McpClientError``（基礎類）：HTTP 已接受連線但回傳無法解析的內容（非 JSON / SSE 無
  data 行）——既非 timeout 也非 unavailable 的啟動窗口。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# MCP 協定版本（對齊上游 mcp SDK 支援值）。
_PROTOCOL_VERSION = "2025-03-26"


class McpClientError(Exception):
    """mcp_client 的基礎例外。"""


class McpTimeout(McpClientError):
    """HTTP 有連上但逾時——橋活著、daemon 未在時限內回應。"""


class McpUnavailable(McpClientError):
    """連不上 loopback HTTP listener——子行程 HTTP 尚未就緒或已掛掉。"""


@dataclass
class McpCallResult:
    """tools/call 的結果封裝。

    - ``ok``：工具是否成功（無 JSON-RPC error 且 result.isError 非 true）。
    - ``payload``：成功時的 tools/call result（含 content）。
    - ``kind``：失敗分類——opendesign health 無 RBAC，一律 ``"tool_error"``。
    - ``message``：人類可讀的錯誤/結果文字。
    """

    ok: bool
    payload: Any = None
    kind: str = ""
    message: str = ""


def _parse_response(resp: httpx.Response) -> dict:
    """解析 JSON 或 SSE-framed 的 JSON-RPC 回應，回傳 message dict。"""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[len("data:") :].strip()
                if not data:
                    continue
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if isinstance(msg, dict) and ("result" in msg or "error" in msg or "id" in msg):
                    return msg
        raise McpClientError("SSE 回應中找不到可解析的 JSON-RPC data 行")
    try:
        return resp.json()
    except ValueError as exc:
        raise McpClientError(f"回應非合法 JSON：{exc}") from exc


def _extract_text(result: dict) -> str:
    """把 tools/call result 的 content 陣列串成純文字（供錯誤訊息與展示）。"""
    parts: list[str] = []
    for item in result.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)


async def call_tool_via_mcp(
    port: int,
    tool: str,
    arguments: dict,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> McpCallResult:
    """對 loopback ``/mcp`` 走 initialize→initialized→tools/call，回 McpCallResult。

    ``transport`` 僅供測試注入 ``httpx.MockTransport``。逾時 / 連線失敗分別拋
    ``McpTimeout`` / ``McpUnavailable``（呼叫端據此分層誠實表述）。
    """
    url = f"http://127.0.0.1:{port}/mcp"
    base_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    client_kwargs: dict = {"timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            # --- (1) initialize ---
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "opendesign-mcp-admin", "version": "1.0"},
                },
            }
            resp = await client.post(url, headers=base_headers, json=init_req)
            _parse_response(resp)  # 確認 initialize 回應可解析
            session_id = resp.headers.get("mcp-session-id")

            sess_headers = dict(base_headers)
            if session_id:
                sess_headers["Mcp-Session-Id"] = session_id

            # --- (2) notifications/initialized（通知、無 id）---
            await client.post(
                url,
                headers=sess_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

            # --- (3) tools/call ---
            call_req = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
            resp = await client.post(url, headers=sess_headers, json=call_req)
            msg = _parse_response(resp)
    except httpx.TimeoutException as exc:
        raise McpTimeout(str(exc)) from exc
    except httpx.ConnectError as exc:
        raise McpUnavailable(str(exc)) from exc
    except httpx.RequestError as exc:
        # 其他傳輸層錯誤（ReadError/RemoteProtocolError 等）歸為 unavailable。
        raise McpUnavailable(str(exc)) from exc

    # --- 解讀 tools/call 結果 ---
    if "error" in msg:
        err = msg["error"]
        text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return McpCallResult(ok=False, kind="tool_error", message=text)

    result = msg.get("result", {})
    if isinstance(result, dict) and result.get("isError"):
        return McpCallResult(ok=False, kind="tool_error", message=_extract_text(result))

    return McpCallResult(
        ok=True,
        payload=result,
        message=_extract_text(result) if isinstance(result, dict) else "",
    )
