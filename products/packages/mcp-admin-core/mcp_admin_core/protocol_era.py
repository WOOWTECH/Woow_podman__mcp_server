"""PaaS 對外暫時只提供 MCP 2025 世代（``initialize`` 握手）：2026-07-28 專屬方法在閘道回 -32601（#78）。

MCP Python SDK 2.x 的 server 在同一個端點同時服務兩個世代：

- 2025 世代（2024-11-05～2025-11-25）：先 ``initialize`` 握手，之後帶 ``Mcp-Session-Id``；
- 2026-07-28 世代：沒有握手，client 先送 ``server/discover``，之後每個請求自帶 ``_meta`` 與 header，
  沒有 server→client 的反向通道；``subscriptions/listen`` 取代原本的 GET 串流。

閘道（``proxy.py``）把 ``/mcp`` 直接轉給子行程，所以子行程升到 SDK 2.x 後，會照 ``server/discover`` 自動協商
的 client（真實 Claude.ai 連線時就是先送它，見評估報告 §8.4；mcp 2.x 的 ``Client`` 預設 ``mode="auto"`` 也是）
會被升級到 2026-07-28 世代——這條路徑還沒有用真實 client 驗證過。在驗證之前，閘道自己回答 2026-07-28 專屬的
client 請求：HTTP 200＋JSON-RPC ``-32601 Method not found``（通知則 202、不轉發），與 HA 版閘道
（WOOWTECH/Woow_ha_mcp_addons 0.1.13 起）相同。依 SDK 2.x client 的協商規則（``mcp/client/_probe.py``：
除了「只支援 2026 世代」的 -32022 以外，任何 JSON-RPC 錯誤都退回 ``initialize``），client 會改走 2025 世代握手，
與 SDK 1.x 子行程時代相同。

範圍：所有產品（子行程是 SDK 2.x、TS SDK、Go 或 supergateway 都一樣，後幾者本來就不認得這兩個方法）、
Streamable HTTP 與根路徑；legacy SSE 的 ``/messages/`` 不攔（回應走 GET 串流，2026 世代 client 也不會走 SSE）。
只看單一 JSON-RPC 訊息；batch 原樣轉發（2025-06-18 起 MCP 已移除 batch）。明確指定 2026-07-28、不送
``server/discover`` 的 client（例如 mcp 2.x ``Client(mode="2026-07-28")``）不受影響，照舊由子行程服務。
"""

from __future__ import annotations

from typing import Any

#: 只存在於 MCP 2026-07-28 世代的 client 請求（mcp-types 2.3.0 的 ``CLIENT_REQUESTS``：2026-07-28 新增
#: ``server/discover``、``subscriptions/listen``；``initialize``、``ping``、``logging/setLevel``、
#: ``resources/subscribe``／``unsubscribe`` 則只在 2025 世代）。
MODERN_ONLY_METHODS = frozenset({"server/discover", "subscriptions/listen"})

#: JSON-RPC 2.0 Method not found。
METHOD_NOT_FOUND = -32601


def modern_only_message(rpc: Any) -> dict | None:
    """*rpc* 是單一 2026-07-28 專屬方法的 JSON-RPC 訊息時回傳該訊息，否則 None。"""
    if isinstance(rpc, dict) and rpc.get("jsonrpc") == "2.0" and rpc.get("method") in MODERN_ONLY_METHODS:
        return rpc
    return None


def method_not_found(message: dict) -> dict | None:
    """請求（有 id）回 JSON-RPC -32601 的內容；通知（沒有 id）回 None（呼叫端回 202）。"""
    if "id" not in message:
        return None
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": METHOD_NOT_FOUND, "message": "Method not found"}}
