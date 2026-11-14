"""MCP reverse proxy — replaces the nginx auth proxy.

Routes ``/private_{token}/{path}`` requests to the MCP server on
localhost, validating the token against the config store.  Supports
streaming responses required by the MCP protocol (SSE, chunked).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from . import gateway_pool, protocol_era, request_body, session_reclaim, setup_status, tool_filter, operation_policy
from .mcp_access import AccessChanged, AccessLease, LeasedResponse, LeasedStreamingResponse
from .config import get_config_store
from .launch_policy import coerce_port
from .process import get_process_manager
from .request_limits import RequestLimitError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-proxy"])

#: ``tools/call`` 從收到請求到「工具結果送完」的總期限（秒）。Cloudflare 對單一請求的原點回應上限是
#: 120 秒，超過就回一頁 HTML 的 524，工具也沒有任何伺服器端上限或提示（DATA-OMCP-007-3：
#: search_records 讀 14 個 JS asset 的 datas 要 125 秒）。在這之前先由 proxy 回 JSON-RPC 錯誤，
#: 用戶端拿到可讀的指引。``MCP_TOOL_CALL_TIMEOUT=0`` 關閉。
#:
#: 期限同時涵蓋回應標頭與回應本體：Streamable HTTP 子行程（mcp FastMCP 預設 ``json_response=False``，
#: odoo-mcp 也是）收到 ``tools/call`` 會**立刻**回 ``200 text/event-stream`` 標頭，工具結果之後才以
#: SSE 事件送達——只等標頭的期限永遠不會觸發。SSE 回應在期限到時改送一個 JSON-RPC 錯誤事件並結束串流
#: （``SseToolsFilter`` 只吐完整事件，所以錯誤事件不會插在半個事件中間）。
TOOL_CALL_TIMEOUT = float(os.environ.get("MCP_TOOL_CALL_TIMEOUT", "110"))
#: JSON-RPC 2.0 實作定義的伺服器錯誤區間（-32000 ~ -32099）。
TOOL_TIMEOUT_ERROR_CODE = -32001
#: 閘道自己產生的其他錯誤（同一區間；不用 -32002，那是 MCP 的 resource not found）：
#: 這個帳號送往子行程的連線或串流名額已滿（gateway_pool.py）。
GATEWAY_BUSY_ERROR_CODE = -32003
#: 子行程在處理這個請求途中斷線（當掉、被 kill）。
CHILD_INTERRUPTED_ERROR_CODE = -32004
#: 回應送到一半時 MCP token 被輪替或撤銷（mcp_access.LeasedStreamingResponse 補的結尾）。
ACCESS_CHANGED_ERROR_CODE = -32005
#: 名額已滿時請用戶端多久之後再試（秒，Retry-After）。
BUSY_RETRY_AFTER_SECONDS = 5
STREAM_LIMIT_RETRY_AFTER_SECONDS = 30
CHILD_INTERRUPTED_MESSAGE = (
    "The MCP server process stopped while handling this request (it restarts automatically). The operation may "
    "or may not have completed; check before retrying. (MCP 子行程在處理這個請求時中斷（會自動重新啟動），"
    "這個操作可能已完成也可能沒有，請先確認再重試。)"
)
ACCESS_CHANGED_MESSAGE = (
    "The MCP token was rotated or revoked while this request was running, so the gateway stopped the response. "
    "Reconnect with the current MCP URL. (處理這個請求時 MCP token 已被輪替或撤銷，閘道已停止這個回應；"
    "請用目前的 MCP 網址重新連線。)"
)
#: 用戶端在請求內容送完之前就斷線（上傳中斷）時記下的狀態碼（nginx 的 499 Client Closed Request；用戶端已經不在，
#: 實際上送不出去，只給 access log 看）。
CLIENT_CLOSED_REQUEST = 499
INITIALIZE_READY_TIMEOUT = 10.0
INITIALIZE_READY_WAITERS = 32
# There is no authenticated loopback peer identity or per-generation address.
# Keep automatic session DELETE and initialize replay off until that boundary
# has a reviewed fence. This is not configurable by requests or environment.
_AUTOMATIC_RECLAIM_EXPERIMENTAL = False


class _ChildNotReady(Exception):
    """The captured managed child changed or did not become ready."""


class _ChildUnroutable(Exception):
    """No identity-verified managed generation owns the child port: nothing is forwarded (blocker 3).

    Answered exactly like an unreachable child (setup placeholder, 502 or 503), without connecting.
    """


def _same_generation(left, right) -> bool:
    return left[0] is right[0] and left[1] is right[1]


def _dispose_unclaimed_initialize(task):
    """Join a late transport without releasing its managed generation early."""
    if task.cancelled():
        return
    try:
        response = task.result()
    except BaseException:
        return
    async def close():
        try:
            await response.aclose()
        except Exception:
            pass
    asyncio.create_task(close())


async def _client_disconnect(request):
    while (await request.receive()).get('type') != 'http.disconnect':
        pass


def _check_initialize_generation(pm, generation):
    proc, changed = generation
    current_proc, current_changed = pm.initialize_generation()
    if (changed.is_set() or proc is None or proc.returncode is not None
            or current_proc is not proc or current_changed is not changed):
        raise _ChildNotReady


async def _generation_send(pm, generation, lease, send):
    """Fence each side-effecting initialize/reclaim I/O against managed replacement."""
    if not pm.begin_initialize_dispatch(generation):
        raise _ChildNotReady
    try:
        lease.check()
        response = await send()
        try:
            _check_initialize_generation(pm, generation)
        except _ChildNotReady:
            await response.aclose()
            raise
        return response
    except asyncio.CancelledError:
        _check_initialize_generation(pm, generation)
        raise
    finally:
        pm.end_initialize_dispatch()


async def _bounded_initialize_send(lease, send, timeout, request):
    operation = asyncio.create_task(lease.run(send()))
    disconnect = asyncio.create_task(_client_disconnect(request))
    try:
        done, _ = await asyncio.wait((operation, disconnect), timeout=max(0.0, timeout),
                                     return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done:
            raise ClientDisconnect
        if operation not in done:
            raise _ChildNotReady
        return operation.result()
    except BaseException:
        if not operation.done():
            operation.cancel()
        operation.add_done_callback(_dispose_unclaimed_initialize)
        raise
    finally:
        disconnect.cancel()
        await asyncio.gather(disconnect, return_exceptions=True)


async def _bounded_setup_fields(store, lease, deadline):
    """Read-only setup lookup; at most one abandoned lookup per store.

    A cancellation-resistant lookup must not extend an initialize deadline or
    allow an unbounded number of background lookups. None means unknown.
    """
    _require_lease(lease)
    prior = getattr(store, "_initialize_setup_lookup", None)
    if prior is not None and not prior.done():
        return None
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return None
    operation = asyncio.create_task(setup_status.missing_fields(store))
    store._initialize_setup_lookup = operation
    def finished(task):
        if getattr(store, "_initialize_setup_lookup", None) is task:
            store._initialize_setup_lookup = None
        if not task.cancelled():
            task.exception()
    operation.add_done_callback(finished)
    revoked = asyncio.create_task(lease.changed.wait())
    try:
        done, _ = await asyncio.wait((operation, revoked), timeout=remaining,
                                     return_when=asyncio.FIRST_COMPLETED)
        _require_lease(lease)
        if operation not in done:
            return None
        return operation.result()
    finally:
        revoked.cancel()
        if not operation.done():
            operation.cancel()


async def _wait_for_initialize_ready(pm, lease, request, deadline):
    """Wait for an existing probe, without owning or cancelling that probe."""
    capture = getattr(pm, 'initialize_generation', None)
    task = getattr(pm, '_configuration_task', None)
    if not capture:
        return None
    if getattr(pm, 'is_running', False) and capture()[1].is_set():
        raise _ChildNotReady
    if not getattr(pm, '_verifying_configuration', False) or task is None:
        if (getattr(pm, 'is_running', False) and getattr(pm, '_attempted_revision', None) is not None
                and getattr(pm, '_applied_revision', None) != pm._attempted_revision):
            raise _ChildNotReady
        return None
    generation = capture()
    proc, changed = generation
    revision = pm._attempted_revision
    if proc is None or proc.returncode is not None or changed.is_set() or revision is None:
        raise _ChildNotReady
    if pm._initialize_waiters >= INITIALIZE_READY_WAITERS:
        raise _ChildNotReady
    pm._initialize_waiters += 1

    async def wait():
        shared = asyncio.shield(task)
        invalidated = asyncio.create_task(changed.wait())
        disconnect = asyncio.create_task(_client_disconnect(request))
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait((shared, invalidated, disconnect), timeout=remaining,
                                         return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                raise ClientDisconnect
            if not done or invalidated in done or shared.cancelled():
                raise _ChildNotReady
            try:
                await shared
            except Exception:
                raise _ChildNotReady from None
            lease.check()
            current_proc, current_event = pm.initialize_generation()
            if (changed.is_set() or current_proc is not proc or current_event is not changed or proc.returncode is not None
                    or pm._attempted_revision != revision or pm._applied_revision != revision):
                raise _ChildNotReady
            return generation
        finally:
            shared.cancel()
            invalidated.cancel()
            disconnect.cancel()
            await asyncio.gather(shared, invalidated, disconnect, return_exceptions=True)

    try:
        return await lease.run(wait())
    finally:
        pm._initialize_waiters -= 1


async def _wait_for_forwardable(pm, routable, port, lease, request, deadline):
    """A just-spawned generation whose OS identity check is still running: wait for it (bounded).

    Before blocker 3 such a request was forwarded as soon as anything listened on the port. Now it is
    forwarded only after the manager verified the listener; waiting here keeps a request that arrives right
    after a (re)start from failing with 502 a few milliseconds before the check passes. Returns when the
    generation is forwardable or there is nothing to wait for (stopped, changed, check finished, deadline);
    the send itself checks again.
    """
    proc, changed = pm.initialize_generation()
    if (routable(port) is not None or proc is None or proc.returncode is not None or changed.is_set()
            or not getattr(pm, '_verifying_configuration', False)):
        return

    async def verified():
        while (routable(port) is None and pm._verifying_configuration and pm._process is proc
               and proc.returncode is None and not changed.is_set()):
            await asyncio.sleep(0.02)

    waiter = asyncio.create_task(lease.run(verified()))
    disconnect = asyncio.create_task(_client_disconnect(request))
    try:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        done, _ = await asyncio.wait((waiter, disconnect), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done:
            raise ClientDisconnect
        if waiter in done:
            waiter.result()  # AccessChanged when the token changed meanwhile
    finally:
        waiter.cancel()
        disconnect.cancel()
        await asyncio.gather(waiter, disconnect, return_exceptions=True)


def _tool_timeout_errors(rpc) -> list[dict]:
    msgs = [m for m in tool_filter.messages(rpc) if m.get("method") == "tools/call"]
    text = (
        f"Tool call exceeded {int(TOOL_CALL_TIMEOUT)} seconds and was stopped by the gateway "
        "(Cloudflare drops requests after 120 s). Retry with a smaller limit, fewer fields "
        "(avoid binary fields such as 'datas'), or a narrower filter. "
        f"(工具呼叫超過 {int(TOOL_CALL_TIMEOUT)} 秒，已由閘道中止；請縮小 limit、減少欄位"
        "（避免 datas 等二進位欄位）或收窄條件後重試。)"
    )
    return [
        {"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": TOOL_TIMEOUT_ERROR_CODE, "message": text}}
        for m in msgs
    ]


def _tool_timeout_response(rpc) -> JSONResponse:
    errors = _tool_timeout_errors(rpc)
    return JSONResponse(errors if isinstance(rpc, list) else errors[0])


def _tool_timeout_sse(rpc) -> bytes:
    """期限到時插進 SSE 串流的錯誤事件（每個 tools/call 一個 JSON-RPC 回應）。"""
    return b"".join(
        b"event: message\ndata: " + json.dumps(err, ensure_ascii=False).encode() + b"\n\n"
        for err in _tool_timeout_errors(rpc)
    )


def _request_ids(rpc) -> list:
    """要回覆的請求的 id（通知與回應不必回）。只看 POST 的 JSON-RPC 內容；GET／DELETE 傳 None。"""
    return [m["id"] for m in tool_filter.messages(rpc)
            if isinstance(m.get("method"), str) and m.get("id") is not None]


def _rpc_error(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _rpc_errors(rpc, ids, code: int, message: str):
    """每個 id 一則 JSON-RPC 錯誤：batch 回 list、單一請求回 dict。"""
    errors = [_rpc_error(rpc_id, code, message) for rpc_id in ids]
    return errors if isinstance(rpc, list) else errors[0]


def _gateway_error(rpc, *, status: int, code: int, message: str, retry_after: int | None = None,
                   ok_with_ids: bool = False) -> JSONResponse:
    """閘道自己回答的錯誤（請求沒有拿到子行程的回應）：帶得出請求 id 就逐一帶上，否則 id 為 null。

    ``ok_with_ids``：請求本身沒問題、session 也還在（例如名額已滿）時，有 id 就回 HTTP 200＋JSON-RPC 錯誤——與
    request_body.py 相同的理由：MCP Python SDK 1.x client 遇到非 2xx 會讓整個 session 拋例外，這樣只有這次呼叫失敗。
    只用在 Streamable HTTP：legacy SSE 的 ``/messages/`` 回應走 GET 串流，POST 只看狀態碼（與 request_body 相同）。
    """
    ids = _request_ids(rpc)
    if ids:
        payload = _rpc_errors(rpc, ids, code, message)
        if ok_with_ids:
            status = 200
    else:
        payload = _rpc_error(None, code, message)
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(payload, status_code=status, headers=headers)


def _busy_message(limits: gateway_pool.GatewayLimits | None) -> str:
    if limits is None:  # httpx 自己的共用池（保險用的上限）也滿了
        return ("All gateway connections to the MCP server are in use, so this request was not sent. Retry shortly. "
                "(閘道送往 MCP 子行程的連線都在使用中，這個請求沒有送出，請稍後再試。)")
    return (f"All {limits.max_connections} gateway connections of this MCP URL are in use; waited "
            f"{limits.pool_timeout:g} s without a free one, so this request was not sent. Retry shortly. "
            f"(這個 MCP 網址送往 MCP 子行程的 {limits.max_connections} 條連線都在使用中，等了 {limits.pool_timeout:g} 秒"
            "仍沒有空出來，這個請求沒有送出，請稍後再試。)")


def _stream_limit_message(limits: gateway_pool.GatewayLimits) -> str:
    return (f"This MCP URL already has {limits.max_streams} open event streams (GET), the per-profile limit. Close "
            "MCP clients that are no longer used, or retry later; requests (POST) still work. "
            f"(這個 MCP 網址已經開著 {limits.max_streams} 條事件串流（GET），是每個帳號的上限；請關閉不再使用的 MCP "
            "用戶端或稍後再試，一般請求（POST）不受影響。)")


_SSE_EVENT_BOUNDARY = re.compile(rb"\r\n\r\n|\n\n|\r\r")
#: 判斷 SSE 事件是不是某個請求的回應時，最多整段解析這麼大的事件；更大的只看開頭與結尾（MCP Python SDK 與 TS SDK
#: 序列化的欄位順序），看不出來就當成還沒回應（寧可多送一則同 id 的錯誤，也不要讓用戶端一直等）。
_RESPONSE_PARSE_LIMIT = 64 * 1024
_ID = rb'(-?\d+|"(?:[^"\\]|\\.)*")'
_HEAD_WITH_ID = re.compile(rb'\s*\{\s*"jsonrpc"\s*:\s*"2\.0"\s*,\s*"id"\s*:\s*' + _ID + rb'\s*,\s*"(?:result|error)"\s*:')
_HEAD_RESULT = re.compile(rb'\s*\{\s*"(?:result|error)"\s*:')
_TAIL_ID = re.compile(rb'"id"\s*:\s*' + _ID + rb'\s*\}\s*\Z')


def _id_key(rpc_id) -> str:
    return json.dumps(rpc_id)


def _answered_ids(chunk: bytes) -> list[str]:
    """這段 SSE（完整事件）裡回應了哪些請求 id（:func:`_id_key` 形式）。"""
    keys = []
    for event in _SSE_EVENT_BOUNDARY.split(chunk):
        data = b"\n".join(line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:"))
        if not data:
            continue
        if len(data) <= _RESPONSE_PARSE_LIMIT:
            for message in tool_filter.messages(tool_filter.parse_jsonrpc(data)):
                if "id" in message and "method" not in message and ("result" in message or "error" in message):
                    keys.append(_id_key(message["id"]))
            continue
        match = _HEAD_WITH_ID.match(data, 0, 512)
        if match is None and _HEAD_RESULT.match(data, 0, 64):
            tail = data[-512:]
            position = tail.rfind(b'"id"')
            match = _TAIL_ID.match(tail, position) if position >= 0 else None
        if match is not None:
            try:
                keys.append(_id_key(json.loads(match.group(1))))
            except ValueError:
                pass
    return keys


class _Interruption:
    """轉發中的回應被中途切斷（子行程斷線、token 換掉）時，閘道補給用戶端的結尾。

    只記「已經交給下游」的內容：``stream_body`` 在 ``yield`` 之後（下游送完、要下一段時）才呼叫 :meth:`delivered`。
    SSE：每個還沒看到回應的請求補一則 JSON-RPC 錯誤事件（用戶端不必等到自己的逾時）；JSON：本體一個位元組都還沒送時
    回 JSON-RPC 錯誤本體，送了一部分就只能結束（不能在半個 JSON 後面接東西）。GET 串流沒有請求 id，不補任何東西。
    """

    def __init__(self, rpc, *, sse: bool) -> None:
        self._batch = isinstance(rpc, list)
        self._sse = sse
        self._pending = {_id_key(rpc_id): rpc_id for rpc_id in _request_ids(rpc)}
        self._bytes = 0

    def delivered(self, chunk: bytes) -> None:
        self._bytes += len(chunk)
        if self._sse and self._pending:
            for key in _answered_ids(chunk):
                self._pending.pop(key, None)

    def tail(self, code: int, message: str) -> bytes:
        pending = list(self._pending.values())
        if self._sse:
            return b"".join(b"event: message\ndata: " + json.dumps(_rpc_error(rpc_id, code, message),
                                                                    ensure_ascii=False).encode() + b"\n\n"
                            for rpc_id in pending)
        if self._bytes == 0 and pending:
            errors = [_rpc_error(rpc_id, code, message) for rpc_id in pending]
            return json.dumps(errors if self._batch else errors[0], ensure_ascii=False).encode()
        return b""


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or (mapped is not None and mapped.is_loopback)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4：去掉 ``.``／``..``，停在根目錄（不會爬出子行程的根）。"""
    output: list[str] = []
    segments = path.split("/")[1:]
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == ".":
            if last:
                output.append("")
        elif segment == "..":
            if output:
                output.pop()
            if last:
                output.append("")
        else:
            output.append(segment)
    return "/" + "/".join(output)


def _relative_reference(base_path: str, target_path: str) -> str:
    """子行程路徑空間裡，從 ``base_path``（這次請求）到 ``target_path`` 的相對參照（不含任何前綴）。"""
    base_dirs = base_path.split("/")[1:-1]
    target = target_path.split("/")[1:]
    target_dirs, last = target[:-1], target[-1]
    common = 0
    while common < len(base_dirs) and common < len(target_dirs) and base_dirs[common] == target_dirs[common]:
        common += 1
    reference = "/".join([".."] * (len(base_dirs) - common) + target_dirs[common:] + [last])
    if not reference or reference.startswith("/") or ":" in reference.split("/", 1)[0]:
        reference = "./" + reference
    return reference


def _external_location(location: str, child_path: str) -> str:
    """子行程回的 ``Location`` 指向它自己（``http://127.0.0.1:<port>/…`` 或 ``/…``）時，改寫成相對參照（P3-c）。

    相對參照以這次請求的網址為基準，用戶端自己解析成 ``/private_<token>/…``，所以回應裡既沒有內部位址，也沒有
    token。指向其他主機的網址、本來就是相對路徑的參照原樣保留。
    """
    try:
        parts = urlsplit(location)
    except ValueError:
        return location
    if parts.scheme or parts.netloc:
        if parts.scheme.lower() not in ("http", "https", "") or not _is_loopback(parts.hostname):
            return location
    elif not parts.path.startswith("/"):
        return location
    reference = _relative_reference(child_path, _remove_dot_segments(parts.path or "/"))
    if parts.query:
        reference += "?" + parts.query
    if parts.fragment:
        reference += "#" + parts.fragment
    return reference

# Shared client with long timeout for MCP streaming calls
_client: httpx.AsyncClient | None = None


class _PolicyRefused(Exception):
    """A fresh product policy refused the call before upstream I/O began."""
    def __init__(self, errors, batch):
        self.payload = errors if batch else errors[0]


def _get_client(timeout: float = 86400) -> httpx.AsyncClient:
    """所有帳號共用的 client：一個連線池，但每個帳號有自己的連線／串流名額、等名額有上限（gateway_pool.py，QA-1）。"""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = gateway_pool.new_client(timeout)
    return _client


async def _read_stream_disabled(store):
    disabled, _ = operation_policy.read_policy(await store.get("tools", {}))
    return disabled


def _require_lease(lease) -> None:
    """閘道自己回答之前再確認一次 token 沒有在這段期間被輪替或撤銷。"""
    try:
        lease.check()
    except AccessChanged:
        raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")


def _is_initialize(request: Request, rpc) -> bool:
    """單一的 initialize 請求（上游 isInitializeRequest 也只認單一訊息，不認 batch）。"""
    return request.method == "POST" and isinstance(rpc, dict) and rpc.get("method") == "initialize"


def _observe_session(sessions, request: Request, rpc, session_id, generation, upstream_resp) -> None:
    """依上游回應更新 session 紀錄：initialize 成功開始記錄；DELETE 成功或上游回 404 就移除。"""
    status = upstream_resp.status_code
    if _is_initialize(request, rpc) and 200 <= status < 300:
        sessions.created(upstream_resp.headers.get("mcp-session-id"), generation)
    elif session_id and (status == 404 or (request.method == "DELETE" and 200 <= status < 300)):
        sessions.forget(session_id)


async def _buffered(upstream_resp, lease) -> tuple[httpx.Response, bytes]:
    """把上游回應整個讀完（429 這種小回應），回傳可以照原樣交給用戶端的複本與內容。"""
    try:
        raw = await lease.run(upstream_resp.aread())
    finally:
        await upstream_resp.aclose()
    replay = httpx.Response(
        upstream_resp.status_code,
        headers=[(key, value) for key, value in upstream_resp.headers.multi_items()
                 if key.lower() not in ("content-encoding", "content-length", "transfer-encoding")],
        content=raw,
    )
    return replay, raw


async def _reclaim_and_retry(upstream_resp, send_upstream, *, lease, store, sessions, generation, client, pm,
                             upstream_url, forward_headers):
    """上游對 initialize 回 429：是「session 名額已滿」就回收最久沒活動的閒置 session，再重試。

    呼叫前 token 已驗證（只有通過 MCP token 驗證的請求才會走到這裡）。回傳要交給用戶端的上游回應：重試的結果，
    或（不是名額已滿／沒有可回收的 session／額度用完）最後一個 429 的內容。重試又拿到「名額已滿」＝剛釋放的名額
    被沒有經過回收鎖的 initialize 先用掉了：用同一份 DELETE 額度再回收下一個。見 session_reclaim.py。
    """
    replay, raw = await _buffered(upstream_resp, lease)
    if not sessions.is_session_limit(upstream_resp.status_code, raw):
        return replay
    lifetime = sessions.lifetime(await store.get("connection", {}))
    delete_url = upstream_url.split("?", 1)[0]
    delete_headers = {"host": forward_headers["host"], "accept": "application/json, text/event-stream"}
    if forward_headers.get("authorization"):
        delete_headers["authorization"] = forward_headers["authorization"]

    async def delete(session_id: str) -> int:
        delete_request = client.build_request(
            "DELETE", delete_url, headers={**delete_headers, "mcp-session-id": session_id},
            timeout=session_reclaim.DELETE_TIMEOUT_SECONDS,
        )
        response = await lease.run(_generation_send(pm, generation, lease, lambda: client.send(delete_request)))
        await response.aclose()
        return response.status_code

    async with sessions.serialized():
        budget = sessions.budget()
        while True:
            lease.check()
            _check_initialize_generation(pm, generation)
            reclaimed = await sessions.reclaim(delete, generation, lifetime, budget=budget)
            _check_initialize_generation(pm, generation)
            if not reclaimed:
                return replay
            lease.check()
            retried = await lease.run(send_upstream())
            if retried.status_code != 429:
                return retried
            replay, raw = await _buffered(retried, lease)
            if not sessions.is_session_limit(retried.status_code, raw):
                return replay
            logger.info("%s session 名額在回收與重試之間被別的 initialize 用掉了，再回收下一個", sessions.label)


@router.api_route(
    "/private_{token}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def mcp_proxy(token: str, path: str, request: Request) -> StreamingResponse:
    """Validate the URL-path token and reverse-proxy to the MCP server."""
    store = get_config_store()
    try:
        lease = await AccessLease.acquire(store, token)
    except AccessChanged:
        raise HTTPException(status_code=403, detail="Invalid or revoked MCP auth token")

    # Build upstream URL
    mcp_cfg = await store.get("mcp_server", {})
    mcp_port = mcp_cfg.get("port", 8000)
    # P3-c：結尾多一個斜線（/private_<token>/mcp/）也是 Streamable HTTP 端點，與下面判斷 streamable／initialize 的寫法一致。
    # 以前原樣轉給子行程，Starlette 回 307 轉到 http://127.0.0.1:<子行程 port>/mcp：露出內部位址，用戶端也連不上。
    upstream_path = "mcp" if path.strip("/") == "mcp" else path
    upstream_url = f"http://127.0.0.1:{mcp_port}/{upstream_path}"

    # Forward query string
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Build headers — forward most, drop hop-by-hop + DNS-rebinding-sensitive。
    # FastMCP 子行程的 TransportSecurityMiddleware（DNS-rebinding 保護）同時驗 Host 與
    # Origin。本 proxy 是受信任的 in-container loopback client：
    #   - Host：改寫成 127.0.0.1:{mcp_port}（見下；pattern 需帶 port，裸 "localhost" 回 421）。
    #   - Origin：外部瀏覽器型 MCP client 會送 https://<host>（非 loopback）→ 子行程回 403
    #     Invalid Origin header（與 Host 的 421 分開）。loopback 子行程不該以瀏覽器 Origin
    #     判信任，故 drop（不放進 skip 後再轉發）；FastMCP 對「缺 Origin」短路放行
    #     （transport_security.py:_validate_origin `if not origin: return True`）。
    #     ⇒ 外層 McpOriginMiddleware 必須先依部署 allowlist 驗證外部 Origin；這裡仍不透傳給子行程。
    forward_headers = {}
    # Caller-provided profile hints and bearer credentials must never select or
    # authenticate the backend. URL token -> saved profile -> saved credentials.
    skip = {"host", "origin", "connection", "transfer-encoding", "keep-alive",
            "authorization", "x-mcp-profile"}
    for key, value in request.headers.items():
        if key.lower() not in skip:
            forward_headers[key] = value
    forward_headers["host"] = f"127.0.0.1:{mcp_port}"

    # Add bearer token if configured (n8n proxy pattern)
    proxy_cfg = await store.get("proxy", {})
    bearer = proxy_cfg.get("bearer_token")
    if bearer:
        forward_headers["authorization"] = f"Bearer {bearer}"

    # 輸入界線：先完成 token 驗證，且所有 proxy 路徑都在轉發前套用。n8n 用產品自己的界線（McpRequestLimits：
    # 4 MiB＋參數元素／深度）；其他產品用共用的請求內容上限（16 MiB，與 Python 子行程的 SDK 上限相同，
    # 見 request_body.py）——超過就不轉發、不整包放進記憶體，回可讀的 JSON-RPC 錯誤（知道請求 id 時
    # 200＋帶 id，client 只有這次呼叫失敗；否則 413）。legacy SSE 的 POST（/messages/）只看狀態碼。
    legacy_sse = path.startswith("messages")
    limits = getattr(request.app.state, "mcp_request_limits", None)
    try:
        body = await lease.run(limits.read_body(request) if limits else
                               request_body.read_body(request, legacy_sse=legacy_sse))
        rpc = limits.parse_and_check(body) if limits else (
            tool_filter.parse_jsonrpc(body) if request.method == "POST" else None
        )
    except RequestLimitError as exc:
        return JSONResponse(exc.payload, status_code=exc.status)
    except AccessChanged:
        raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
    except ClientDisconnect:
        # 用戶端在請求內容送完之前就斷線（上傳中斷、網路斷掉、Cloudflare 那端放棄）：不是伺服器錯誤，也沒有東西要轉發。
        # 以前一路拋到 uvicorn，記成 ERROR「Exception in ASGI application」加 190 行 traceback（正式站 145、194）。
        # 這裡只記一行 warning：方法與 token 之後的路徑（不含 token），不帶 traceback。
        logger.warning("用戶端在請求內容送完之前中斷連線（%s /%s），沒有轉發", request.method, path)
        return Response(status_code=CLIENT_CLOSED_REQUEST)

    # 子行程沒在跑時由閘道回答的佔位 MCP（setup_status.py）：佔位 session 是閘道自己發的，不轉發、不經 Tool Manager。
    # 子行程真的起來（在聽 port）之後回 404，用戶端依規格重新 initialize，拿到真的 session 與完整工具。
    streamable = not legacy_sse and path.strip("/") == "mcp"
    if streamable and setup_status.owns_session(request.headers.get("mcp-session-id")):
        answer = await setup_status.placeholder_session_answer(request, rpc, store=store, port=coerce_port(mcp_port))
        _require_lease(lease)
        return answer

    # 名額已滿時的 session 回收（宣告了 SessionReclaimer 的產品，目前是 n8n；見 session_reclaim.py）：只記錄
    # Streamable HTTP 的 /mcp。帶 Mcp-Session-Id 的 POST／DELETE 從這裡（token 已驗過）開始算進行中，回應送完才結束。
    # GET 的 SSE 串流不算使用中、也不算活動：與上游一樣（上游只在 POST 更新 lastAccess，開著 GET 串流的 session 照樣
    # 逾時清掉），而真的 Claude Code 連上後一直開著 GET 串流，算使用中的話執行中的 Claude Code 永遠回收不到。
    sessions = getattr(getattr(request, "state", None), "session_reclaimer",
                       getattr(request.app.state, "session_reclaimer", None))
    if not streamable:
        sessions = None
    session_id = request.headers.get("mcp-session-id") if sessions is not None else None
    pm = get_process_manager()
    capture = getattr(pm, 'initialize_generation', None)
    managed_generation = capture() if capture and pm.is_running else None
    # Blocker 3: a manager forwards only to the generation whose listener passed its OS identity check.
    # (Objects without this hook are duck-typed test doubles of the old interface.)
    routable = getattr(pm, 'forward_generation', None)
    generation = managed_generation if managed_generation is not None else (pm.child_pid if sessions is not None else None)
    finish = sessions.begin(session_id, generation) if sessions is not None and request.method != "GET" else None
    try:
        response = await _relay(
            request, path, token, store=store, lease=lease, body=body, rpc=rpc, legacy_sse=legacy_sse,
            limits=limits, forward_headers=forward_headers, upstream_url=upstream_url, proxy_cfg=proxy_cfg,
            sessions=sessions, session_id=session_id, generation=generation, pm=pm,
            managed_generation=managed_generation, routable=routable, child_port=coerce_port(mcp_port),
        )
    except BaseException:
        if finish is not None:
            finish()
        raise
    if finish is not None:
        if isinstance(response, (LeasedResponse, LeasedStreamingResponse)):
            response.on_close = finish  # 回應（含 SSE 串流）送完或用戶端斷線時才結束
        else:
            finish()
    return response


async def _relay(request: Request, path: str, token: str, *, store, lease, body, rpc, legacy_sse, limits,
                 forward_headers, upstream_url, proxy_cfg, sessions, session_id, generation, pm, managed_generation,
                 routable=None, child_port=None):
    """token 已驗、請求已讀完之後：協定世代、停用的工具／操作、轉發上游與回應處理。

    *routable*（``McpProcessManager.forward_generation``）在每次上游送出之前同步檢查：只有通過 OS 身分檢查、
    還是目前世代的子行程收得到請求；否則一律不轉發（blocker 3）。
    """
    # #78：MCP 2026-07-28 世代專屬方法（server/discover、subscriptions/listen）由閘道回 -32601、不轉發，
    # 讓自動協商的 client（Claude.ai 會先送 server/discover）退回 2025 世代的 initialize 握手（見 protocol_era.py）。
    modern = protocol_era.modern_only_message(rpc) if request.method == "POST" and not legacy_sse else None
    if modern is not None:
        try:
            lease.check()
        except AccessChanged:
            raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
        reply = protocol_era.method_not_found(modern)
        if reply is None:
            return Response(status_code=202, headers={"Cache-Control": "no-store"})
        return JSONResponse(reply, headers={"Cache-Control": "no-store"})

    # #1384：Tool Manager 停用的工具在 proxy 層強制擋下（子行程本身不認 tools.disabled）
    tools_config = await store.get("tools", {})
    disabled, _ = operation_policy.read_policy(tools_config)
    registry = getattr(request.app.state, "tool_registry", None)
    canonical_arguments = getattr(registry, "normalize_arguments", None) is not None

    def check_wire_size(value):
        # n8n canonical escaping/batch rewriting may grow an accepted UTF-8
        # request. Apply its existing bound to the actual final wire bytes too.
        if canonical_arguments and limits and len(value) > limits.max_bytes:
            raise RequestLimitError(f"MCP request exceeds {limits.max_bytes} bytes",
                                    rpc_id=rpc.get("id") if isinstance(rpc, dict) else None)

    blocked_tools = tool_filter.disabled_calls(rpc, disabled)
    blocked_ops = operation_policy.disabled_operation_calls(rpc, tools_config, registry)
    if canonical_arguments and tool_filter.requests_method(rpc, "tools/call"):
        # n8n's child can replace output-wrapped arguments. Never authorize a
        # canonical call and then forward the original, differently interpreted
        # bytes. Bounds/auth have already run; other products keep their bytes.
        body = operation_policy.canonical_call_bytes(rpc)
        forward_headers.pop("content-length", None)
    denied_ops = {id(message) for message in blocked_ops}
    denied = {id(message) for message in blocked_tools} | denied_ops
    blocked = [message for message in tool_filter.messages(rpc) if id(message) in denied]
    if blocked:
        try:
            lease.check()
        except AccessChanged:
            raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
        all_blocked = len(blocked) == len(tool_filter.messages(rpc))
        if all_blocked and not path.startswith("messages"):
            errors = [operation_policy.operation_error(message) if id(message) in denied_ops
                      else tool_filter.disabled_error(message) for message in blocked]
            return JSONResponse(errors if isinstance(rpc, list) else errors[0])
        # Legacy SSE replies on the GET stream. An empty tool name cannot name
        # a registered MCP tool; preserve IDs and the other batch members.
        body = operation_policy.rewrite_blocked_calls(rpc, blocked, canonical=canonical_arguments)
        forward_headers.pop("content-length", None)
    wants_tools_list = rpc is not None and tool_filter.requests_method(rpc, "tools/list")

    try:
        check_wire_size(body)
    except RequestLimitError as exc:
        return JSONResponse(exc.payload, status_code=exc.status)
    client = _get_client(proxy_cfg.get("timeout", 86400))

    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + TOOL_CALL_TIMEOUT
        if TOOL_CALL_TIMEOUT > 0 and rpc is not None and tool_filter.requests_method(rpc, "tools/call")
        else None
    )

    single_initialize = not legacy_sse and path.strip('/') == 'mcp' and _is_initialize(request, rpc)
    forwarded = None  # the verified generation this request was actually sent to
    try:
        startup_deadline = loop.time() + INITIALIZE_READY_TIMEOUT
        startup_generation = (await _wait_for_initialize_ready(pm, lease, request, startup_deadline)
                              if single_initialize else None)
        if startup_generation is not None and startup_generation != managed_generation:
            raise _ChildNotReady
        if single_initialize and managed_generation is None:
            # Never forward into an unowned or newly started process. Preserve
            # the setup placeholder only while a real manager reports stopped.
            if getattr(pm, 'initialize_generation', None) and not pm.is_running:
                missing = await _bounded_setup_fields(store, lease, startup_deadline)
                _require_lease(lease)
                if missing is not None and not pm.is_running:
                    return setup_status.answer(request, rpc, missing)
            raise _ChildNotReady
        fenced_generation = managed_generation if single_initialize else None
        if not single_initialize and routable is not None and getattr(pm, 'is_running', False):
            await _wait_for_forwardable(pm, routable, child_port, lease, request,
                                        startup_deadline if deadline is None else min(deadline, startup_deadline))

        async def send_upstream():
            nonlocal forwarded
            current_body = body
            if canonical_arguments and tool_filter.requests_method(rpc, "tools/call"):
                # AccessLease schedules I/O in a task. A policy save can win
                # that scheduling boundary; re-read inside it, not from the
                # child's possibly stale env. Normalization is idempotent.
                latest = await store.get("tools", {})
                latest_disabled, _ = operation_policy.read_policy(latest)
                latest_tools = tool_filter.disabled_calls(rpc, latest_disabled)
                latest_ops = operation_policy.disabled_operation_calls(rpc, latest, registry)
                op_ids = {id(m) for m in latest_ops}
                ids = {id(m) for m in latest_tools} | op_ids
                latest_blocked = [m for m in tool_filter.messages(rpc) if id(m) in ids]
                if latest_blocked and len(latest_blocked) == len(tool_filter.messages(rpc)) and not path.startswith("messages"):
                    raise _PolicyRefused([
                        operation_policy.operation_error(m) if id(m) in op_ids else tool_filter.disabled_error(m)
                        for m in latest_blocked], isinstance(rpc, list))
                current_body = (operation_policy.rewrite_blocked_calls(rpc, latest_blocked, canonical=True) if latest_blocked
                                else operation_policy.canonical_call_bytes(rpc))
                forward_headers.pop("content-length", None)
            check_wire_size(current_body)
            lease.check()
            if routable is not None:
                # Synchronously before the send: never a stale, fenced, unverified or foreign listener.
                target = routable(child_port)
                if target is None or (fenced_generation is not None and not _same_generation(target, fenced_generation)):
                    raise _ChildNotReady if fenced_generation is not None else _ChildUnroutable
                forwarded = target
            upstream_req = client.build_request(
                method=request.method, url=upstream_url, headers=forward_headers,
                content=current_body if current_body else None,
            )
            if fenced_generation is None:
                return await client.send(upstream_req, stream=True)
            return await _generation_send(pm, fenced_generation, lease,
                                          lambda: client.send(upstream_req, stream=True))

        if startup_generation is not None:
            upstream_resp = await _bounded_initialize_send(lease, send_upstream, startup_deadline - loop.time(), request)
        elif deadline is not None:
            upstream_resp = await asyncio.wait_for(
                lease.run(send_upstream()), timeout=deadline - loop.time()
            )
        else:
            upstream_resp = await lease.run(send_upstream())
        if (_AUTOMATIC_RECLAIM_EXPERIMENTAL and startup_generation is None
                and fenced_generation is not None and sessions is not None and upstream_resp.status_code == 429
                and _is_initialize(request, rpc)):
            upstream_resp = await _reclaim_and_retry(
                upstream_resp, send_upstream, lease=lease, store=store, sessions=sessions, generation=generation,
                client=client, pm=pm, upstream_url=upstream_url, forward_headers=forward_headers,
            )
        if fenced_generation is not None:
            try:
                _check_initialize_generation(pm, fenced_generation)
            except _ChildNotReady:
                await upstream_resp.aclose()
                raise
    except RequestLimitError as exc:
        return JSONResponse(exc.payload, status_code=exc.status)
    except _PolicyRefused as exc:
        try:
            lease.check()
        except AccessChanged:
            raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
        return JSONResponse(exc.payload)
    except _ChildNotReady:
        if single_initialize:
            # A child may exit while checking a fresh, incomplete setup. Give
            # the caller the local setup session without forwarding anything;
            # a configured generation change still fails closed with 503.
            missing = await _bounded_setup_fields(store, lease, startup_deadline)
            if missing:
                _require_lease(lease)
                return setup_status.answer(request, rpc, missing)
        _require_lease(lease)
        raise HTTPException(503, detail="MCP child is not ready; reconnect after checking its status")
    except ClientDisconnect:
        return Response(status_code=CLIENT_CLOSED_REQUEST)
    except AccessChanged:
        raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
    except asyncio.TimeoutError:
        logger.warning("tools/call exceeded %ss; returned a JSON-RPC timeout error", TOOL_CALL_TIMEOUT)
        return _tool_timeout_response(rpc)
    except (httpx.ConnectError, _ChildUnroutable) as exc:
        if isinstance(exc, httpx.ConnectError) and forwarded is not None:
            # A verified child stopped accepting: re-check who owns the port before anyone else can take it.
            recheck = getattr(pm, "peer_unreachable", None)
            if recheck is not None:
                await recheck(forwarded, child_port)
        # 子行程沒在跑（開機因缺連線設定 exit、當掉、自動重啟中）：Streamable HTTP 由閘道回答最小 MCP 伺服器，說明原因與
        # 去哪裡設定（setup_status.py；缺必要連線欄位時「在跑」的只是看門狗的重啟嘗試，一樣回設定步驟）。其他路徑
        # （legacy SSE）照舊 503＋可讀原因（#1350）；子行程在跑但連不上（啟動窗口等）維持 502。
        if not legacy_sse and path.strip("/") == "mcp":
            if single_initialize:
                missing = await _bounded_setup_fields(store, lease, startup_deadline)
                _require_lease(lease)
                if missing is None:
                    raise HTTPException(503, detail="MCP child is not ready; reconnect after checking its status")
                answer = (setup_status.answer(request, rpc, missing) if missing or not pm.is_running else None)
            else:
                answer = await setup_status.unreachable_answer(request, rpc, store=store)
            if answer is not None:
                _require_lease(lease)
                return answer
            # Do not replay a startup connection failure: a child may have
            # been replaced. A client retry is a fresh authenticated request.
            pm = get_process_manager()
            if (_is_initialize(request, rpc) and pm.is_running
                    and getattr(pm, "_verifying_configuration", False)):
                _require_lease(lease)
                raise HTTPException(503, detail="MCP child is starting; retry initialization",
                                    headers={"Retry-After": "1"})
        elif not get_process_manager().is_running:
            raise HTTPException(
                503,
                detail=(
                    "MCP server is not running — open the admin page to check the connection "
                    "settings (MCP 子行程未運行，請至管理頁檢查連線設定並儲存)"
                ),
            )
        raise HTTPException(502, detail="MCP server not reachable")
    except gateway_pool.StreamLimitReached as exc:
        # 這個帳號開著的 GET 串流已達上限（QA-1）：不等、不轉發。串流沒有 JSON-RPC id，用戶端只看狀態碼與說明。
        _require_lease(lease)
        return _gateway_error(rpc, status=429, code=GATEWAY_BUSY_ERROR_CODE, message=_stream_limit_message(exc.limits),
                              retry_after=STREAM_LIMIT_RETRY_AFTER_SECONDS)
    except httpx.PoolTimeout as exc:
        # 這個帳號的連線名額（或保險用的共用池）在期限內都沒有空出來：請求沒有送出，不是子行程逾時（QA-1）。
        _require_lease(lease)
        return _gateway_error(rpc, status=503, code=GATEWAY_BUSY_ERROR_CODE,
                              message=_busy_message(getattr(exc, "limits", None)),
                              retry_after=BUSY_RETRY_AFTER_SECONDS, ok_with_ids=not legacy_sse)
    except httpx.TimeoutException:
        raise HTTPException(504, detail="MCP server timed out")
    except httpx.TransportError as exc:
        # 連上了，子行程卻在回應標頭之前斷線（當掉、被 kill：httpx.ReadError、RemoteProtocolError、WriteError）。
        # 以前沒接住：uvicorn 回 500 並記約 163 行 traceback（2026-11-13 多帳號實測 res R2c，P3-b）。現在記一行
        # warning（方法與 token 之後的路徑，不含 token、不帶 traceback），回 502＋JSON-RPC 錯誤（帶得出 id 就帶）。
        _require_lease(lease)
        logger.warning("轉發途中子行程的連線中斷（%s /%s，%s），回 502", request.method, path, type(exc).__name__)
        return _gateway_error(rpc, status=502, code=CHILD_INTERRUPTED_ERROR_CODE, message=CHILD_INTERRUPTED_MESSAGE)

    if sessions is not None:
        _observe_session(sessions, request, rpc, session_id, generation, upstream_resp)

    if upstream_resp.status_code == 413 and request.method == "POST":
        # 子行程拒收過大的請求（mcp SDK 回純文字 "Request body too large"，client 只看到看不懂的 413）：
        # 換成可讀的 JSON-RPC 錯誤，帶得到 id 就讓 client 只這次呼叫失敗；子行程的內容不轉出。
        await upstream_resp.aclose()
        try:
            lease.check()
        except AccessChanged:
            raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
        content, status = request_body.child_refusal(rpc, legacy_sse=legacy_sse)
        return JSONResponse(content, status_code=status)

    # Stream response back — disable buffering for SSE
    resp_headers = dict(upstream_resp.headers)
    for h in ("transfer-encoding", "content-encoding", "content-length"):
        resp_headers.pop(h, None)
    resp_headers["x-accel-buffering"] = "no"
    if "location" in resp_headers:
        # P3-c：子行程的轉址指向它自己（http://127.0.0.1:<port>/…）：改成相對參照，不露出內部位址、也不放進 token。
        resp_headers["location"] = _external_location(resp_headers["location"], urlsplit(upstream_url).path)

    content_type = upstream_resp.headers.get("content-type") or ""
    # 回應被中途切斷時閘道補的結尾（子行程斷線或 token 換掉；見 _Interruption）。legacy SSE 的 POST（/messages/）
    # 回應走 GET 串流，這個 POST 的回應內容用戶端不看，不補。
    interruption = _Interruption(rpc if request.method == "POST" and not legacy_sse else None,
                                 sse=content_type.startswith("text/event-stream"))

    def access_changed_tail() -> bytes:
        return interruption.tail(ACCESS_CHANGED_ERROR_CODE, ACCESS_CHANGED_MESSAGE)

    if wants_tools_list and disabled and content_type.startswith("application/json"):
        # tools/list 的 JSON 回應有界，整包讀完後移除被停用的工具
        try:
            raw = await lease.run(upstream_resp.aread())
            lease.check()
        except AccessChanged:
            raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
        except httpx.TransportError as exc:
            _require_lease(lease)
            logger.warning("轉發途中子行程的連線中斷（%s /%s，%s），回 502", request.method, path, type(exc).__name__)
            return _gateway_error(rpc, status=502, code=CHILD_INTERRUPTED_ERROR_CODE,
                                  message=CHILD_INTERRUPTED_MESSAGE)
        finally:
            await upstream_resp.aclose()
        return LeasedResponse(
            tool_filter.filter_json_body(raw, disabled),
            lease=lease,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=content_type,
            interrupted=access_changed_tail,
        )

    # legacy SSE：子行程的 endpoint 事件是 loopback 根路徑（/messages/?session_id=…），
    # 對外要補上 /private_<token>，否則 client 的 POST 打到 admin 根路徑（DATA-NMCP-005／AI-HMCP-002）
    sse_filter = (
        tool_filter.SseToolsFilter(
            lambda: _read_stream_disabled(store),
            endpoint_prefix=f"{request.scope.get('root_path', '')}/private_{token}",
        )
        if content_type.startswith("text/event-stream")
        else None
    )

    # 只有 SSE 能在串流中途補一個 JSON-RPC 錯誤事件；JSON 回應的標頭本來就等結果算完才送（上面已計時）。
    body_deadline = deadline if sse_filter is not None else None

    async def stream_body():
        chunks = upstream_resp.aiter_bytes()
        try:
            while True:
                try:
                    if body_deadline is None:
                        chunk = await lease.run(chunks.__anext__())
                    else:
                        chunk = await asyncio.wait_for(
                            lease.run(chunks.__anext__()), timeout=max(body_deadline - loop.time(), 0)
                        )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning(
                        "tools/call exceeded %ss while streaming; sent a JSON-RPC timeout error", TOOL_CALL_TIMEOUT
                    )
                    yield _tool_timeout_sse(rpc)
                    return
                except httpx.TransportError as exc:
                    # 標頭已送出後子行程斷線（當掉、被 kill）：狀態碼改不了。SSE 補每個還沒得到回應的請求一則 JSON-RPC
                    # 錯誤事件（SseToolsFilter 只吐完整事件，不會插在半個事件中間；沒送完的半個事件丟掉），JSON 本體還沒送出
                    # 任何位元組時回 JSON-RPC 錯誤，然後正常結束。以前一路拋到 uvicorn（約 163 行 traceback，P3-b）。
                    lease.check()
                    logger.warning("子行程的回應在轉發途中中斷（%s /%s，%s），已結束這個回應",
                                   request.method, path, type(exc).__name__)
                    tail = interruption.tail(CHILD_INTERRUPTED_ERROR_CODE, CHILD_INTERRUPTED_MESSAGE)
                    if tail:
                        yield tail
                    return
                if sse_filter is not None:
                    chunk = await sse_filter.feed(chunk)
                    if not chunk:
                        continue
                lease.check()
                yield chunk
                interruption.delivered(chunk)  # 下游送完這一段、要下一段時才到這裡
            if sse_filter is not None:
                rest = sse_filter.flush()
                if rest:
                    lease.check()
                    yield rest
                    interruption.delivered(rest)
        finally:
            # AccessChanged 不在這裡吞掉：交給 LeasedStreamingResponse 補閘道的結尾（403 或 JSON-RPC 錯誤），
            # 不要讓回應看起來是完整地結束了。
            await upstream_resp.aclose()

    if fenced_generation is not None:
        # The outer ASGI boundary checks this again immediately before sending
        # headers; BaseHTTPMiddleware can schedule other tasks in between.
        request.scope.setdefault('state', {})['_mcp_initialize_guard'] = (pm, fenced_generation, lease)
    return LeasedStreamingResponse(
        stream_body(),
        lease=lease,
        upstream=upstream_resp,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
        interrupted=access_changed_tail,
    )


# Bare /private_TOKEN (no trailing path) also needs to work
@router.api_route(
    "/private_{token}",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def mcp_proxy_root(token: str, request: Request) -> StreamingResponse:
    """Proxy root path (no trailing slash)."""
    return await mcp_proxy(token, "", request)
