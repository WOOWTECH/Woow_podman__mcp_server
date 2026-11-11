"""MCP reverse proxy — replaces the nginx auth proxy.

Routes ``/private_{token}/{path}`` requests to the MCP server on
localhost, validating the token against the config store.  Supports
streaming responses required by the MCP protocol (SSE, chunked).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from . import protocol_era, request_body, session_reclaim, setup_status, tool_filter, operation_policy
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
#: 用戶端在請求內容送完之前就斷線（上傳中斷）時記下的狀態碼（nginx 的 499 Client Closed Request；用戶端已經不在，
#: 實際上送不出去，只給 access log 看）。
CLIENT_CLOSED_REQUEST = 499


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

# Shared client with long timeout for MCP streaming calls
_client: httpx.AsyncClient | None = None


class _PolicyRefused(Exception):
    """A fresh product policy refused the call before upstream I/O began."""
    def __init__(self, errors, batch):
        self.payload = errors if batch else errors[0]


def _get_client(timeout: float = 86400) -> httpx.AsyncClient:
    global _client  # noqa: PLW0603
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
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


async def _reclaim_and_retry(upstream_resp, send_upstream, *, lease, store, sessions, generation, client,
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
        response = await lease.run(client.send(delete_request))
        await response.aclose()
        return response.status_code

    async with sessions.serialized():
        budget = sessions.budget()
        while True:
            lease.check()
            if not await sessions.reclaim(delete, generation, lifetime, budget=budget):
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
    upstream_url = f"http://127.0.0.1:{mcp_port}/{path}"

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
    #     ⇒ MCP endpoint 不得透傳外部 Origin。
    forward_headers = {}
    skip = {"host", "origin", "connection", "transfer-encoding", "keep-alive"}
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
    sessions = getattr(request.app.state, "session_reclaimer", None)
    if not streamable:
        sessions = None
    session_id = request.headers.get("mcp-session-id") if sessions is not None else None
    generation = get_process_manager().child_pid if sessions is not None else None
    finish = sessions.begin(session_id, generation) if sessions is not None and request.method != "GET" else None
    try:
        response = await _relay(
            request, path, token, store=store, lease=lease, body=body, rpc=rpc, legacy_sse=legacy_sse,
            limits=limits, forward_headers=forward_headers, upstream_url=upstream_url, proxy_cfg=proxy_cfg,
            sessions=sessions, session_id=session_id, generation=generation,
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
                 forward_headers, upstream_url, proxy_cfg, sessions, session_id, generation):
    """token 已驗、請求已讀完之後：協定世代、停用的工具／操作、轉發上游與回應處理。"""
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

    try:
        async def send_upstream():
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
            upstream_req = client.build_request(
                method=request.method, url=upstream_url, headers=forward_headers,
                content=current_body if current_body else None,
            )
            return await client.send(upstream_req, stream=True)

        if deadline is not None:
            upstream_resp = await asyncio.wait_for(
                lease.run(send_upstream()), timeout=deadline - loop.time()
            )
        else:
            upstream_resp = await lease.run(send_upstream())
        if sessions is not None and upstream_resp.status_code == 429 and _is_initialize(request, rpc):
            upstream_resp = await _reclaim_and_retry(
                upstream_resp, send_upstream, lease=lease, store=store, sessions=sessions, generation=generation,
                client=client, upstream_url=upstream_url, forward_headers=forward_headers,
            )
    except RequestLimitError as exc:
        return JSONResponse(exc.payload, status_code=exc.status)
    except _PolicyRefused as exc:
        try:
            lease.check()
        except AccessChanged:
            raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
        return JSONResponse(exc.payload)
    except AccessChanged:
        raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
    except asyncio.TimeoutError:
        logger.warning("tools/call exceeded %ss; returned a JSON-RPC timeout error", TOOL_CALL_TIMEOUT)
        return _tool_timeout_response(rpc)
    except httpx.ConnectError:
        # 子行程沒在跑（開機因缺連線設定 exit、當掉、自動重啟中）：Streamable HTTP 由閘道回答最小 MCP 伺服器，說明原因與
        # 去哪裡設定（setup_status.py；缺必要連線欄位時「在跑」的只是看門狗的重啟嘗試，一樣回設定步驟）。其他路徑
        # （legacy SSE）照舊 503＋可讀原因（#1350）；子行程在跑但連不上（啟動窗口等）維持 502。
        if not legacy_sse and path.strip("/") == "mcp":
            answer = await setup_status.unreachable_answer(request, rpc, store=store)
            if answer is not None:
                _require_lease(lease)
                return answer
        elif not get_process_manager().is_running:
            raise HTTPException(
                503,
                detail=(
                    "MCP server is not running — open the admin page to check the connection "
                    "settings (MCP 子行程未運行，請至管理頁檢查連線設定並儲存)"
                ),
            )
        raise HTTPException(502, detail="MCP server not reachable")
    except httpx.TimeoutException:
        raise HTTPException(504, detail="MCP server timed out")

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

    content_type = upstream_resp.headers.get("content-type") or ""

    if wants_tools_list and disabled and content_type.startswith("application/json"):
        # tools/list 的 JSON 回應有界，整包讀完後移除被停用的工具
        try:
            raw = await lease.run(upstream_resp.aread())
            lease.check()
        except AccessChanged:
            raise HTTPException(403, detail="MCP access changed; reconnect using the current shared token")
        finally:
            await upstream_resp.aclose()
        return LeasedResponse(
            tool_filter.filter_json_body(raw, disabled),
            lease=lease,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=content_type,
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
                if sse_filter is not None:
                    chunk = await sse_filter.feed(chunk)
                    if not chunk:
                        continue
                lease.check()
                yield chunk
            if sse_filter is not None:
                rest = sse_filter.flush()
                if rest:
                    lease.check()
                    yield rest
        except AccessChanged:
            return
        finally:
            await upstream_resp.aclose()

    return LeasedStreamingResponse(
        stream_body(),
        lease=lease,
        upstream=upstream_resp,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
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
