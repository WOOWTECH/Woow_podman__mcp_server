"""由產品明確啟用的 MCP 輸入界線；在驗證 token 後、轉發／SDK 驗證前執行。"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import json
import re
from typing import Any

from fastapi import Request


#: 超過上限的請求，閘道最多再讀（丟棄）到這裡為止才回應（n8n 的 :class:`McpRequestLimits` 與共用 16 MiB 上限
#: ``request_body.read_body`` 都用）。比 Cloudflare 的 100 MB 上限大，經 Cloudflare／反向代理進來的請求都會讀到結尾。
#: 一定要先讀完：還沒讀完就回應並關連線（用戶端帶 ``Connection: close``，或反向代理對上游這樣連）時，核心會因為還有
#: 未讀的資料送 RST，「先送完內容再讀回應」的用戶端只看到 ECONNRESET、看不到錯誤（第二輪驗證 E2：經 kubectl
#: port-forward 送 8 MiB）。更大的（只可能來自叢集內）不等它傳完，直接拒收。
DRAIN_BYTES = 128 * 1024 * 1024


#: 結構守門（:func:`structure_violation`）的預設上限，給沒有自己宣告 :class:`McpRequestLimits` 的產品用（``create_app``）。
#: 位元組預算（request_budget.py）只算 body 的位元組，但 JSON 解析成 Python 物件後記憶體會依結構膨脹：16 MiB 的
#: ``[[],[],…]``、``[{},{}…]`` 解析後多 386～403 MiB（24～25 倍），純字串只有 1 倍。所以另外限制「容器巢狀深度」與「元素個數」，
#: 讓結構化 body 的最壞放大倍數壓在預算假設內（量測見 docs/mcp-gateway-limits.md）。
#:
#: 元素個數＝逗號數＋容器（``[``、``{``）數（字串內的不算）：每個值幾乎都對應一個逗號或一個容器開頭。
#: 上限 200,000：Odoo ``execute_method`` 批次 create 3,000 筆 × 30 欄位約 93,000，仍有 2 倍餘裕；而 16 MiB 的最壞 body
#: 在這個上限下解析後只多約 30 MiB（見量測），不再是 400 MiB。大字串（Nextcloud 13.3 MiB 的 base64 檔案）不含任何逗號或括號，
#: 元素數 0，不受影響。
DEFAULT_MAX_TOTAL_ELEMENTS = 200_000
DEFAULT_MAX_DEPTH = 64

# 一次比對「一段不含結構字元的內容（字串整段、含跳脫，其餘字元）＋一個結構字元」：Python 迴圈的次數＝結構字元數（超過上限就停），
# 不是 body 的位元組數。possessive 量詞：長字串一次吃完、不回溯。
_RUN_THEN_STRUCTURE = re.compile(rb'(?:"(?:[^"\\]+|\\.)*+"|[^"\[\]{},]++)*+([\[\]{},])', re.DOTALL)
_OPENERS = frozenset(b"[{")
_COMMA = ord(",")


#: 檢查「整段是合法 UTF-8」時每次解碼的區塊大小：不把整份內容解成 str（astral 字元會讓 str 變成 4 倍），暫存只有區塊大小。
_UTF8_CHUNK = 1024 * 1024
NOT_UTF8_REASON = "the body is not UTF-8 text (UTF-16 and UTF-32 are not accepted)"


def not_utf8(body: bytes) -> bool:
    """body 不是 UTF-8 就回 True（P1-A，第二輪審查 F1）。

    ``json.loads(bytes)`` 依 RFC 4627 看前 4 個位元組有沒有 NUL（或 BOM）自動改用 UTF-16／32 解碼；下面的結構掃描卻是逐位元組
    當 UTF-8 掃，UTF-16 裡 ``"`` 是 ``00 22``，引號奇偶全亂，掃描遇到沒結束的字串就放行，``json.loads`` 照樣把整份解開。所以掃描
    之前先拒收：前 4 個位元組含 NUL（UTF-16／32 的偵測條件），或整段不是合法 UTF-8（涵蓋所有 BOM：``ff fe``、``fe ff`` 都不是合法 UTF-8）。

    合法的 UTF-8（中文、日文、emoji、UTF-8 BOM）一定放行：純 ASCII 用 ``isascii()``（C、不複製）；其餘以增量解碼器每次一個區塊
    驗證（線性、暫存常數；跨區塊的多位元組字元由增量解碼器接起來）。"""
    if b"\x00" in body[:4]:
        return True
    if body.isascii():
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    view = memoryview(body)
    try:
        for start in range(0, len(body), _UTF8_CHUNK):
            decoder.decode(view[start:start + _UTF8_CHUNK])
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return True
    return False


def structure_violation(body: bytes, *, max_depth: int, max_elements: int) -> str | None:
    """body（JSON 原始位元組）的容器巢狀深度或元素個數超過上限就回原因（英文短句，不含內容），否則 None。

    元素個數＝逗號數＋容器（``[``、``{``）數；字串內的括號、逗號、跳脫引號都不計。只掃描、不建立任何 JSON 物件，也不解碼
    （不複製整份內容）。

    - 快速路徑：整個 body（含字串內）的 ``,``、``[``、``{`` 次數都沒超過上限時（大字串、檔案內容、一般請求），不可能違規，
      直接放行，成本是三次 ``bytes.count``；
    - 否則逐一掃描結構字元，超過上限就立刻停：最壞情況的成本只跟上限成正比，不跟 body 大小成正比；
    - 先拒收不是 UTF-8 的內容（:func:`not_utf8`；UTF-16／32 會讓下面的掃描失準、``json.loads`` 卻看得懂）；
    - 掃描中閉括號多於開括號（``depth < 0``）就停止、不判違規：那不是合法 JSON，``json.loads`` 本來就會拒絕；沒有這一條時 1,500 萬個
      ``]`` 每個一次 Python 迴圈，掃描卡 5.6 秒（第二輪審查 F2）。合法 JSON 的閉括號不會多於開括號，所以閉括號的迴圈次數不超過
      開括號數（上限內），整個掃描的成本只跟上限成正比（實測 16 MiB 的病態 body 約 0.03 秒）；
    - 掃描中遇到不合 JSON 字串語法的地方（字串沒結束）就停止掃描、不判違規：那不是合法 JSON，之後的解析本來就會失敗。
    """
    if not_utf8(body):
        return NOT_UTF8_REASON
    openers = body.count(b"[") + body.count(b"{")
    if openers + body.count(b",") <= max_elements and openers <= max_depth:
        return None
    depth = elements = pos = 0
    match = _RUN_THEN_STRUCTURE.match
    while (found := match(body, pos)) is not None:
        pos = found.end()
        char = body[pos - 1]
        if char == _COMMA:
            elements += 1
        elif char in _OPENERS:
            elements += 1
            depth += 1
            if depth > max_depth:
                return f"more than {max_depth} levels of nesting"
        else:
            depth -= 1
            if depth < 0:
                return None  # 閉括號比開括號多：不是合法 JSON，之後的解析會拒絕；繼續掃只會被灌閉括號拖慢
        if elements > max_elements:
            return f"more than {max_elements} JSON elements"
    return None


class RequestLimitError(Exception):
    """可直接回傳且不含輸入內容的 HTTP／JSON-RPC 錯誤。"""

    def __init__(self, message: str, *, status: int = 413, code: int = -32602, rpc_id=None, kind: str = "rejected",
                 log_reason: str | None = None):
        """保留已解析的請求 ID，讀取／解析前的拒絕使用 null。

        ``kind``、``log_reason`` 只給閘道記節流 WARNING 用（P2-D）：情況名稱與不含請求內容的短原因。"""
        self.kind = kind
        self.log_reason = log_reason or message
        self.status = status
        self.payload = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
        super().__init__(message)


@dataclass(frozen=True)
class McpRequestLimits:
    """每個 app 固定啟用的請求位元組、工具參數元素與深度上限。"""
    max_bytes: int
    max_argument_elements: int
    max_depth: int = DEFAULT_MAX_DEPTH
    #: 整個請求（不只工具參數）的元素個數上限，見 :data:`DEFAULT_MAX_TOTAL_ELEMENTS`、:func:`structure_violation`。
    max_total_elements: int = DEFAULT_MAX_TOTAL_ELEMENTS
    #: 超過上限時最多再讀（丟棄）多少才回應，見 :data:`DRAIN_BYTES`。
    drain_bytes: int = DRAIN_BYTES

    def structure_reason(self, body: bytes) -> str | None:
        """整個請求的容器深度或元素個數（見 :func:`structure_violation`）超過上限的原因，沒超過回 None。"""
        return structure_violation(body, max_depth=self.max_depth, max_elements=self.max_total_elements)

    def check_structure(self, body: bytes, *, legacy_sse: bool = False) -> None:
        """同 :meth:`structure_reason`，超過就拋 :class:`RequestLimitError`（不含內容）。

        與其他產品（``proxy._parse_unlimited``）同一種拒收格式（第二輪審查 F7）：認得出請求 id 就 HTTP 200＋帶該 id 的 JSON-RPC 錯誤
        （MCP SDK 對非 2xx 會讓整個 session 失敗，這樣只有這次呼叫失敗），認不出才 413。"""
        reason = self.structure_reason(body)
        if reason is not None:
            from . import request_body  # 延後匯入：request_body 匯入本模組的 RequestLimitError

            raise request_body.too_complex(body, reason, legacy_sse=legacy_sse)

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

    def parse_and_check(self, body: bytes, *, legacy_sse: bool = False) -> Any:
        """以迭代器計算 batch 共用預算；回傳未修改的原始訊息。"""
        if not body:
            return None
        self.check_structure(body, legacy_sse=legacy_sse)  # 先於 _check_depth：不是 UTF-8 的內容不能進後面的解碼與解析
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
