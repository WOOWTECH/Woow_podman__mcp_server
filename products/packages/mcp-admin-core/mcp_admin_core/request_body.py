"""MCP 請求內容上限（16 MiB）：Python MCP 子行程與 admin 閘道共用的單一來源。

背景：2026-10-16 起 workspace 的 MCP Python SDK 下限是 1.30.0（GHSA-84m7-p3x7-pcfv，正式映像 5526e865 已是；
2026-10-24 起為 2.x，#78）。1.30.0 起（2.x 相同）的 Streamable HTTP session manager 與 SSE transport 在處理訊息前，
就把超過 ``max_request_body_size``（SDK 預設 4 MiB）的請求內容回 HTTP 413、純文字 ``Request body too large``。
本 repo 原本沒有任何地方設定這個值，
所以 PaaS 上每個 Python MCP 子行程都拒收 4 MiB 以上的請求，閘道再把那個 413 原樣轉給 client；例如 Nextcloud
``upload_file``（檔案以 base64 放在工具參數，產品上限 10 MiB）超過約 3 MiB 的檔案就失敗。

上限依整條路徑每一段的實際上限取 16 MiB（與 HA 版 WOOWTECH/Woow_ha_mcp_addons
``apps/runtime/session_idle.py`` 的 ``BODY_BYTES``＝其閘道 ``MAX_REQUEST`` 相同）：

- Cloudflare（租戶網址前端）：方案的請求上限 100 MB 以上，不是瓶頸；
- cloudflared tunnel、mcp-server chart（ClusterIP-only，沒有 ingress／nginx／auth proxy）、admin 的 uvicorn：
  都不限制請求內容大小；
- admin 閘道（``proxy.py``）：n8n 由 ``McpRequestLimits`` 限 4 MiB（另案審過，不變）；其他產品過去不設限、
  整包讀進記憶體，現在套用本上限（:func:`read_body`）；
- 產品：最大的合法輸入是 Nextcloud ``upload_file`` 的 10 MiB 檔案（``NEXTCLOUD_MCP_MAX_UPLOAD_BYTES``，
  PaaS 的 LaunchPolicy 不開放修改），base64 後約 13.3 MiB 的 JSON，16 MiB 內還有餘裕。

子行程端（每個 Python MCP 子行程的 Streamable HTTP 與 SSE 都套用本上限）：

- core :class:`~mcp_admin_core.mcp_sse_wrapper.DualTransportApp`（hermes、opendesign）明確傳入；
- odoo（odoo-mcp 用 SDK 的 ``MCPServer``）由 launcher 呼叫 :func:`apply_to_run`：mcp 2.x 的 ``MCPServer`` 已沒有
  ``settings.max_request_body_size``（1.x 的 FastMCP 才有），transport 參數改由 ``run()`` 傳入，而且 ``run()`` 一定
  明確傳這個參數（值是 SDK 預設 4 MiB），所以 :func:`install` 對它無效；
- litellm、nextcloud（fastmcp 4.1.3 建 transport 時不傳這個參數、也沒有設定可改；3.4.11 也一樣）由 launcher 呼叫
  :func:`install`，把 SDK 建構子的預設值改成本上限。

超過上限的請求由閘道拒收、不轉發，回可讀的 JSON-RPC 錯誤（寫明上限與處理方式）。MCP Python SDK 的 client
遇到任何 4xx 都直接拋 ``HTTPStatusError``（整個 session 結束、看不到內容），所以能知道請求 id 時一律回
HTTP 200＋帶該 id 的 JSON-RPC 錯誤，讓 client 只有這次呼叫失敗；不知道 id 時才回 413＋id 為 null 的錯誤
（TS SDK 會把內容放進錯誤訊息）。子行程若仍回 413（例如上限較低的非 Python 子行程），閘道同樣改回可讀的
JSON-RPC 錯誤（:func:`child_refusal`），不把子行程的純文字 413 原樣轉給 client。
"""
from __future__ import annotations

import functools
import inspect
import json
import re
from typing import Any

from fastapi import Request

from .request_limits import DRAIN_BYTES, RequestLimitError

#: 每個 Python MCP 子行程與 admin 閘道（n8n 除外）接受的最大請求內容（位元組）。
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024

# 超過上限的請求，閘道最多再讀（丟棄）到 DRAIN_BYTES（request_limits.py，與 n8n 的 McpRequestLimits 共用）為止：
# 一是在結尾找到 JSON-RPC id，二是讀完再回應，用戶端不會因為連線被重設而看不到錯誤。比 Cloudflare 的 100 MB
# 上限大，所以經 Cloudflare 進來的請求都讀得到結尾。更大的（只可能來自叢集內）不等它傳完，直接拒收。

#: 與閘道其他輸入界線相同（``RequestLimitError`` 預設、n8n 的 4 MiB）：JSON-RPC Invalid params。
ERROR_CODE = -32602

_PARAMETER = "max_request_body_size"
_MARK = "_woow_request_body_default"
_MIB = 1024 * 1024


def _size_text(limit: int) -> str:
    return f"{limit // _MIB} MiB ({limit} bytes)" if limit % _MIB == 0 else f"{limit} bytes"


# ---------------------------------------------------------------------------
# 子行程：SDK transport 的預設上限
# ---------------------------------------------------------------------------


def _default_to_ceiling(cls) -> None:
    original = cls.__init__
    if getattr(original, _MARK, False):
        return
    signature = inspect.signature(original)
    if _PARAMETER not in signature.parameters:
        return  # 沒有這個參數的 SDK（1.30.0 以前）也沒有這道上限，維持原樣

    @functools.wraps(original)
    def __init__(self, *args, **kwargs):
        if _PARAMETER not in signature.bind(self, *args, **kwargs).arguments:
            kwargs[_PARAMETER] = MAX_REQUEST_BODY_BYTES
        original(self, *args, **kwargs)

    setattr(__init__, _MARK, True)
    cls.__init__ = __init__


def install() -> None:
    """讓本行程之後建立的 SDK transport 預設使用 :data:`MAX_REQUEST_BODY_BYTES`。

    改 ``StreamableHTTPSessionManager`` 與 ``SseServerTransport`` 建構子的預設值：呼叫端沒傳
    ``max_request_body_size`` 時用本上限，明確傳入的值照用（DualTransportApp、SDK 的 ``MCPServer`` 都會明確傳）。
    給 fastmcp 系的子行程用——fastmcp 4.1.3（與 3.4.11）在 lifespan 裡建 session manager（子類別
    ``FastMCPStreamableHTTPSessionManager`` 以 ``super().__init__`` 呼叫這裡改過的建構子）、建 SSE transport 時都
    不傳這個參數。必須在 server 建立 transport 之前呼叫（launcher 交給 server 之前）；冪等。
    """
    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    _default_to_ceiling(StreamableHTTPSessionManager)
    _default_to_ceiling(SseServerTransport)


def apply_to_run(server: Any, limit: int = MAX_REQUEST_BODY_BYTES) -> None:
    """mcp 2.x 的 ``MCPServer``：之後的 ``server.run("streamable-http"|"sse", ...)`` 沒指定上限時用 *limit*。

    mcp 2.x 把 transport 參數從 ``MCPServer(...)``／``settings`` 搬到 ``run()``，``run()`` 再一路明確傳給 transport
    （所以 :func:`install` 改建構子預設值對它無效）。只包這個 instance 的 ``run``；stdio 不受影響；冪等。
    """
    run = server.run
    if getattr(run, _MARK, False):
        return

    @functools.wraps(run)
    def run_with_ceiling(transport: str = "stdio", **kwargs: Any) -> Any:
        if transport in ("streamable-http", "sse"):
            kwargs.setdefault(_PARAMETER, limit)
        return run(transport, **kwargs)

    setattr(run_with_ceiling, _MARK, True)
    server.run = run_with_ceiling


# ---------------------------------------------------------------------------
# 閘道：讀取上限與可讀的拒收回應
# ---------------------------------------------------------------------------

_ADVICE = ("Send smaller tool arguments: split the work into several calls, and keep uploaded files "
           "within the tool's own file size limit.",
           "請縮小工具參數：拆成多次呼叫，上傳的檔案請在該工具的檔案大小上限內。")

# 沒解析的請求只看頭尾找 id（只認整個 JSON 物件最外層的兩種寫法，避免撿到參數裡的 "id"）：
# - MCP Python／TS SDK 送 ``{"method":…,"params":…,"jsonrpc":"2.0","id":…}``：開頭是 method、結尾是 id；
# - 手寫或其他 SDK 常見 ``{"jsonrpc":"2.0","id":…,"method":…}``：id 在開頭。
_ID = rb'(-?[0-9]{1,19}|"(?:[^"\\\x00-\x1f]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4}){0,256}")'
_HEAD_METHOD = re.compile(rb'\s*\{\s*"method"\s*:\s*"')
_HEAD_ID = re.compile(rb'\s*\{\s*"jsonrpc"\s*:\s*"2\.0"\s*,\s*"id"\s*:\s*' + _ID + rb'\s*,\s*"method"\s*:')
_TAIL_ID = re.compile(rb'[{,]\s*"jsonrpc"\s*:\s*"2\.0"\s*,\s*"id"\s*:\s*' + _ID + rb'\s*\}\s*\Z')
_WINDOW = 4096


def _request_id(head: bytes, tail: bytes) -> Any:
    """沒讀完（或沒解析）的單一請求的 id；認不出來回 None（MCP 的 id 不會是 null）。"""
    match = _HEAD_ID.match(head)
    if match is None and _HEAD_METHOD.match(head):
        match = _TAIL_ID.search(tail)
    if match is None:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _refusal(message: str, ids: list, *, batch: bool, legacy_sse: bool) -> tuple[Any, int]:
    """拒收的 JSON 內容與 HTTP 狀態。

    Streamable HTTP 且知道請求 id：200＋每個請求一個 JSON-RPC 錯誤（client 只讓這次呼叫失敗、session 照常）。
    其他情況（只有通知／回應、認不出 id、legacy SSE 的 ``/messages/``——它的回應走 GET 串流，POST 只看狀態碼）：
    413＋一個 JSON-RPC 錯誤（只有一個請求時帶它的 id，否則 id 為 null）。
    """
    error = {"code": ERROR_CODE, "message": message}
    replies = [{"jsonrpc": "2.0", "id": ident, "error": dict(error)} for ident in ids]
    if replies and not legacy_sse:
        return (replies if batch else replies[0]), 200
    return {"jsonrpc": "2.0", "id": ids[0] if len(ids) == 1 else None, "error": error}, 413


def too_large(head: bytes = b"", tail: bytes = b"", *, legacy_sse: bool,
              limit: int = MAX_REQUEST_BODY_BYTES) -> RequestLimitError:
    """閘道拒收超過 *limit* 的請求；id 從請求頭尾找（見 :func:`_request_id`）。"""
    size = _size_text(limit)
    message = (f"MCP request body exceeds the {size} limit of this MCP server and was not processed. "
               f"{_ADVICE[0]} (MCP 請求內容超過本伺服器 {size} 上限，未處理；{_ADVICE[1]})")
    ident = _request_id(head, tail)
    content, status = _refusal(message, [] if ident is None else [ident], batch=False, legacy_sse=legacy_sse)
    return RequestLimitError(message, status=status, code=ERROR_CODE, rpc_id=content["id"])


async def read_body(request: Request, *, legacy_sse: bool, limit: int = MAX_REQUEST_BODY_BYTES) -> bytes:
    """讀完請求內容（最多 *limit*）；超過就拋 :func:`too_large`，不轉發、也不整包放進記憶體。

    超過上限後繼續讀到結尾但只留頭尾各 4 KiB（為了找 id；client 反正會把內容送完，HTTP server 也會讀掉剩下的），
    超過 :data:`DRAIN_BYTES` 就不再等。
    """
    declared = None
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            declared = None  # 不合法的 Content-Length 交給 HTTP server；這裡以實際讀到的量為準
    if declared is not None and declared > DRAIN_BYTES:
        raise too_large(legacy_sse=legacy_sse, limit=limit)
    body = bytearray()
    head = tail = b""
    total = 0
    over = declared is not None and declared > limit
    async for chunk in request.stream():
        total += len(chunk)
        if not over:
            if total <= limit:
                body.extend(chunk)
                continue
            over = True  # 從這裡起只留頭尾
            head, tail, body = bytes(body[:_WINDOW]), bytes(body[-_WINDOW:]), bytearray()
        if len(head) < _WINDOW:
            head += chunk[:_WINDOW - len(head)]
        tail = (tail + chunk[-_WINDOW:])[-_WINDOW:]
        if total > DRAIN_BYTES:
            raise too_large(head, legacy_sse=legacy_sse, limit=limit)
    if over:
        raise too_large(head, tail, legacy_sse=legacy_sse, limit=limit)
    return bytes(body)


def child_refusal(rpc: Any, *, legacy_sse: bool) -> tuple[Any, int]:
    """子行程回 413 時給 client 的 JSON 內容與 HTTP 狀態，取代子行程的原始回應（SDK 是純文字）。"""
    message = ("The MCP server refused this request because its body is too large (HTTP 413) and did not "
               f"process it. {_ADVICE[0]} (MCP 伺服器拒收：請求內容過大，未處理；{_ADVICE[1]})")
    messages = rpc if isinstance(rpc, list) else [rpc]
    ids = [m["id"] for m in messages if isinstance(m, dict) and "id" in m and "method" in m]
    return _refusal(message, ids, batch=isinstance(rpc, list), legacy_sse=legacy_sse)
