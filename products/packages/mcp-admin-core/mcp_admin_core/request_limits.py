"""由產品明確啟用的 MCP 輸入界線；在驗證 token 後、轉發／SDK 驗證前執行。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from fastapi import Request


#: 超過上限的請求，閘道最多再讀（丟棄）到這裡為止才回應（n8n 的 :class:`McpRequestLimits` 與共用 16 MiB 上限
#: ``request_body.read_body`` 都用）。比 Cloudflare 的 100 MB 上限大，經 Cloudflare／反向代理進來的請求都會讀到結尾。
#: 一定要先讀完：還沒讀完就回應並關連線（用戶端帶 ``Connection: close``，或反向代理對上游這樣連）時，核心會因為還有
#: 未讀的資料送 RST，「先送完內容再讀回應」的用戶端只看到 ECONNRESET、看不到錯誤（第二輪驗證 E2：經 kubectl
#: port-forward 送 8 MiB）。更大的（只可能來自叢集內）不等它傳完，直接拒收。
DRAIN_BYTES = 128 * 1024 * 1024


class RequestLimitError(Exception):
    """可直接回傳且不含輸入內容的 HTTP／JSON-RPC 錯誤。"""

    def __init__(self, message: str, *, status: int = 413, code: int = -32602, rpc_id=None):
        """保留已解析的請求 ID，讀取／解析前的拒絕使用 null。"""
        self.status = status
        self.payload = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
        super().__init__(message)


@dataclass(frozen=True)
class McpRequestLimits:
    """每個 app 固定啟用的請求位元組、工具參數元素與深度上限。"""
    max_bytes: int
    max_argument_elements: int
    max_depth: int = 64
    #: 超過上限時最多再讀（丟棄）多少才回應，見 :data:`DRAIN_BYTES`。
    drain_bytes: int = DRAIN_BYTES

    def _check_depth(self, value: str | bytes) -> None:
        """在建立 JSON 物件前，忽略字串內容計算容器巢狀深度。"""
        # 在 json.loads 建立物件前先掃描結構；字串中的括號、跳脫引號不計入深度。
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        depth = 0
        in_string = escaped = False
        for char in text:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
                if depth > self.max_depth:
                    raise RequestLimitError(f"MCP JSON exceeds {self.max_depth} levels of nesting")
            elif char in "]}":
                depth -= 1

    async def read_body(self, request: Request) -> bytes:
        """同時限制宣告與實際大小；超過上限時不保留內容，但先把剩下的讀完丟掉再拒收。

        先讀完再回應，用戶端（包括在反向代理後面、先送完內容才讀回應的）一定讀得到 413＋JSON-RPC 錯誤，而不是
        連線被重設（見 :data:`DRAIN_BYTES`）。宣告或實際超過 ``drain_bytes`` 的不再等，直接拒收。
        """
        length = request.headers.get("content-length")
        declared = None
        if length is not None:
            try:
                declared = int(length)
                if declared < 0:
                    raise ValueError
            except ValueError:
                raise RequestLimitError("Invalid Content-Length", status=400, code=-32600)
        # 轉發壓縮內容會讓上游解壓後繞過這裡的位元組界線。
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            await self._discard(request, declared)
            raise RequestLimitError("Compressed MCP requests are not supported", status=415)
        if declared is not None and declared > self.max_bytes:
            await self._discard(request, declared)
            raise RequestLimitError(f"MCP request exceeds {self.max_bytes} bytes")
        body = bytearray()
        total = 0
        over = False
        async for chunk in request.stream():
            total += len(chunk)
            if not over and total <= self.max_bytes:
                body.extend(chunk)
                continue
            if not over:
                over = True
                body = bytearray()  # 不保留超量的內容
            if total > self.drain_bytes:
                break
        if over:
            raise RequestLimitError(f"MCP request exceeds {self.max_bytes} bytes")
        return bytes(body)

    async def _discard(self, request: Request, declared: int | None) -> None:
        """把請求內容讀完丟掉（最多 ``drain_bytes``；宣告就超過的一個位元組都不讀）。"""
        if declared is not None and declared > self.drain_bytes:
            return
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > self.drain_bytes:
                return

    def parse_and_check(self, body: bytes) -> Any:
        """以迭代器計算 batch 共用預算；回傳未修改的原始訊息。"""
        if not body:
            return None
        try:
            self._check_depth(body)
            rpc = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise RequestLimitError("Invalid or excessively nested JSON", status=400, code=-32700)
        messages = rpc if isinstance(rpc, list) else [rpc]
        remaining = self.max_argument_elements
        # n8n 會把部分字串參數 JSON.parse 後當結構使用，預算也涵蓋這些解碼後的結構。
        decoded_bytes = 0
        for message in messages:
            if not isinstance(message, dict) or message.get("method") != "tools/call":
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                continue
            stack = [iter((params.get("arguments"),))]
            while stack:
                try:
                    value = next(stack[-1])
                except StopIteration:
                    stack.pop()
                    continue
                if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                    decoded_bytes += len(value.encode("utf-8", errors="surrogatepass"))
                    if decoded_bytes > self.max_bytes:
                        raise RequestLimitError("Encoded MCP arguments exceed decoding budget", rpc_id=message.get("id"))
                    try:
                        self._check_depth(value)
                        value = json.loads(value)
                    except RequestLimitError as error:
                        raise RequestLimitError(
                            error.payload["error"]["message"],
                            status=error.status,
                            code=error.payload["error"]["code"],
                            rpc_id=message.get("id"),
                        ) from None
                    except RecursionError:
                        raise RequestLimitError("Excessively nested MCP arguments", rpc_id=message.get("id"))
                    except ValueError:
                        continue
                if isinstance(value, (dict, list)):
                    if len(stack) > self.max_depth:
                        raise RequestLimitError("Excessively nested MCP arguments", rpc_id=message.get("id"))
                    remaining -= len(value)
                    if remaining < 0:
                        raise RequestLimitError(
                            f"MCP tool arguments exceed {self.max_argument_elements} elements",
                            rpc_id=message.get("id"),
                        )
                    stack.append(iter(value.values() if isinstance(value, dict) else value))
        return rpc
