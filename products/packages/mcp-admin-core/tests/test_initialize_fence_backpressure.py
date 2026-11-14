"""InitializeFence 在背壓下遇到 token 被換掉：乾淨的 403 或 -32005，不拋例外到 uvicorn（#145 審查的非阻擋建議）。

``InitializeFenceMiddleware`` 在最外層送 initialize 回應標頭之前再確認一次 token 與世代，並用 ``lease.run`` 讓「token 被撤銷」
能取消一個被下游背壓卡住的 ``send``。以前 ``lease.run`` 拋出的 ``AccessChanged`` 沒有人接，直接傳到 uvicorn：記成
「Exception in ASGI application」加 traceback、用戶端拿到 500（標頭還沒送出）。

兩種情況：

- 標頭被卡住、還沒交給 transport：閘道自己回 403（與轉發前發現 token 換掉時相同），用戶端拿不到子行程的任何位元組；
- 標頭已經交出去（同一刻 token 被換掉）：狀態碼改不了，不能再送 403；回應照原來的路徑由 ``LeasedStreamingResponse`` 補
  ``-32005`` 的 JSON-RPC 錯誤後正常結束。

兩種都不得拋例外，舊 token 的新請求照舊 403。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from mcp_admin_core.process import McpProcessManager

TOKEN = "synthetic-fence-backpressure"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "fence-test", "version": "1"}}}
SECRET_MARKER = "NEVER_DELIVERED_AFTER_REVOCATION"


def _gateway(make_app, monkeypatch):
    """child 已就緒（沒有探測在跑）的管理程序＋假子行程；initialize 會走有 InitializeFence 保護的路徑。"""
    from mcp_admin_core import proxy

    forwarded: list[str] = []

    async def upstream(request):
        forwarded.append(json.loads(request.content or b"{}").get("method"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"marker": SECRET_MARKER}})

    pm = McpProcessManager()
    pm._process = SimpleNamespace(pid=4242, returncode=None)
    pm._running = True
    # Synthetic stand-in for the probe's OS listener check (MockTransport: no socket to inspect).
    pm._peer_verified = (pm._initialize_generation_changed, 59999)
    monkeypatch.setattr(proxy, "get_process_manager", lambda: pm)
    monkeypatch.setattr(proxy, "_get_client", lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    app = make_app({"mcp_auth_token": TOKEN, "mcp_server": {"command": "", "args": [], "port": 59999, "env": {}}}).app
    return app, pm, forwarded


def _scope():
    path = f"/private_{TOKEN}/mcp"
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"",
            "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234), "server": ("test", 80), "root_path": ""}


async def _revoke():
    from mcp_admin_core.config import get_config_store

    await get_config_store().change_mcp_token(None, action="revoke")


def _body_of(outgoing: list[dict]) -> bytes:
    return b"".join(event.get("body", b"") for event in outgoing if event["type"] == "http.response.body")


@pytest.mark.asyncio
async def test_token_rotated_while_the_headers_are_stuck_gets_a_clean_403_and_no_exception(make_app, monkeypatch):
    app, pm, forwarded = _gateway(make_app, monkeypatch)
    messages: asyncio.Queue = asyncio.Queue()
    await messages.put({"type": "http.request", "body": json.dumps(INIT).encode(), "more_body": False})
    stuck, never = asyncio.Event(), asyncio.Event()
    outgoing: list[dict] = []

    async def receive():
        return await messages.get()

    async def send(event):
        # 外層（uvicorn）被背壓卡住：200 的標頭一直送不出去。拒絕用的 403 不受影響。
        if event["type"] == "http.response.start" and event["status"] == 200:
            stuck.set()
            await never.wait()
        outgoing.append(event)

    task = asyncio.create_task(app(_scope(), receive, send))
    try:
        await asyncio.wait_for(stuck.wait(), 5)
        assert forwarded == ["initialize"]  # 子行程已經收到 initialize（token 換掉之前轉發的）
        await _revoke()
        await asyncio.wait_for(task, 5)  # 以前：AccessChanged 傳出來，uvicorn 記 traceback、回 500
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    starts = [event for event in outgoing if event["type"] == "http.response.start"]
    assert [event["status"] for event in starts] == [403]
    assert SECRET_MARKER.encode() not in _body_of(outgoing)  # 子行程的位元組一個都沒送
    body = json.loads(_body_of(outgoing))
    assert "reconnect" in body["detail"] and TOKEN not in json.dumps(body)
    headers = dict(starts[0]["headers"])
    assert headers[b"content-type"] == b"application/json" and headers[b"cache-control"] == b"no-store"
    assert pm._initialize_dispatches == set()  # 派送的登記有收掉，之後的生命週期步驟不會被卡住


@pytest.mark.asyncio
async def test_token_rotated_in_the_same_instant_the_headers_are_accepted_ends_cleanly(make_app, monkeypatch):
    """標頭交出去的同一刻 token 被換掉：狀態碼已經改不了，不能再送 403，也不能拋例外。回應用它自己的 lease 處理結束：
    子行程的內容若已經通過 lease 檢查、交給下游（在外層被卡住的這段時間裡，它在 BaseHTTPMiddleware 的幾層串流之間排隊），
    照樣完整送出；還沒交出去的，改成閘道的 -32005 JSON-RPC 錯誤。兩種都是完整、合法的 JSON-RPC 回應，正常結束。"""
    app, pm, forwarded = _gateway(make_app, monkeypatch)
    messages: asyncio.Queue = asyncio.Queue()
    await messages.put({"type": "http.request", "body": json.dumps(INIT).encode(), "more_body": False})
    outgoing: list[dict] = []

    async def receive():
        return await messages.get()

    async def send(event):
        outgoing.append(event)
        if event["type"] == "http.response.start":
            await _revoke()  # 標頭交出去的同一刻 token 被換掉

    try:
        await asyncio.wait_for(app(_scope(), receive, send), 5)  # 以前：AccessChanged 傳出來
    finally:
        await messages.put({"type": "http.disconnect"})
    starts = [event for event in outgoing if event["type"] == "http.response.start"]
    assert [event["status"] for event in starts] == [200]  # 標頭已經交出去，狀態碼改不了
    reply = json.loads(_body_of(outgoing))  # 完整、合法的 JSON-RPC 回應（不是截斷的 JSON）
    assert reply["id"] == 1 and ("result" in reply) != ("error" in reply)
    if "error" in reply:
        assert reply["error"]["code"] == -32005
        assert SECRET_MARKER not in json.dumps(reply)
    assert outgoing[-1] == {"type": "http.response.body", "body": b"", "more_body": False}  # 正常結束
    assert pm._initialize_dispatches == set()


@pytest.mark.asyncio
async def test_old_token_stays_rejected_after_the_fence_answered(make_app, monkeypatch):
    app, pm, forwarded = _gateway(make_app, monkeypatch)
    messages: asyncio.Queue = asyncio.Queue()
    await messages.put({"type": "http.request", "body": json.dumps(INIT).encode(), "more_body": False})
    stuck, never = asyncio.Event(), asyncio.Event()

    async def receive():
        return await messages.get()

    async def send(event):
        if event["type"] == "http.response.start" and event["status"] == 200:
            stuck.set()
            await never.wait()

    task = asyncio.create_task(app(_scope(), receive, send))
    try:
        await asyncio.wait_for(stuck.wait(), 5)
        await _revoke()
        await asyncio.wait_for(task, 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    forwarded_before = list(forwarded)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        again = await client.post(f"/private_{TOKEN}/mcp", json=INIT)
    assert again.status_code == 403
    assert forwarded == forwarded_before  # 舊 token 的新請求沒有轉發


# ---------------------------------------------------------------------------
# 獨立審查 N6：例外群組的分流、背壓逾時的行為
# ---------------------------------------------------------------------------


def _leaves(exc: BaseException) -> list[type]:
    """例外（群組）裡所有葉子例外的型別，照出現順序。"""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for member in exc.exceptions for leaf in _leaves(member)]
    return [type(exc)]


async def _run_fence_over(exc: BaseException) -> None:
    """下游應用程式直接拋出 *exc* 時，InitializeFenceMiddleware 往外傳出什麼（沒有例外＝被吞掉）。"""
    from mcp_admin_core.initialize_fence import InitializeFenceMiddleware

    async def app(scope, receive, send):
        raise exc

    await InitializeFenceMiddleware(app)({"type": "http"}, None, None)


def _rejected():
    from mcp_admin_core.initialize_fence import _Rejected

    return _Rejected()


# (名稱, 下游拋出的例外, 傳出去的葉子例外型別；None＝整個吞掉)
_FENCE_CASES = [
    ("only the gateway's own rejection", lambda: _rejected(), None),
    ("a group holding only the rejection", lambda: ExceptionGroup("g", [_rejected()]), None),
    ("a group of rejections", lambda: ExceptionGroup("g", [_rejected(), _rejected()]), None),
    ("a nested group holding only the rejection",
     lambda: ExceptionGroup("g", [ExceptionGroup("h", [_rejected()])]), None),
    ("an ordinary error", lambda: ValueError("boom"), [ValueError]),
    ("a group without the rejection", lambda: ExceptionGroup("g", [ValueError("only")]), [ValueError]),
    ("the rejection together with another error",
     lambda: ExceptionGroup("g", [_rejected(), ValueError("other")]), [ValueError]),
    ("a nested group with the rejection and another error",
     lambda: ExceptionGroup("g", [ExceptionGroup("h", [_rejected()]), KeyError("k")]), [KeyError]),
    ("the rejection together with a cancellation",
     lambda: BaseExceptionGroup("g", [_rejected(), asyncio.CancelledError()]), [asyncio.CancelledError]),
    ("a cancellation", lambda: asyncio.CancelledError(), [asyncio.CancelledError]),
]


@pytest.mark.parametrize("make, expected", [pytest.param(make, expected, id=name) for name, make, expected in _FENCE_CASES])
async def test_only_the_fences_own_rejection_is_swallowed_never_another_error(make, expected):
    """N6（變異 fn05）：下游（BaseHTTPMiddleware 的 task group）可能把 ``_Rejected`` 包在例外群組裡拋出來。只有閘道自己的拒絕
    被吞掉；同一個群組裡的其他例外（以及單獨的例外、取消）照樣傳出去，不能被一起吞掉。"""
    exc = make()
    if expected is None:
        await _run_fence_over(exc)  # 沒有例外
        return
    with pytest.raises(BaseException) as raised:  # noqa: PT011 - 取消與例外群組都要接
        await _run_fence_over(exc)
    assert _leaves(raised.value) == expected
    assert not any(leaf.__name__ == "_Rejected" for leaf in _leaves(raised.value))
    if isinstance(exc, BaseExceptionGroup):  # 傳出去的仍是同一個訊息的例外群組，只少了被分流掉的拒絕
        assert isinstance(raised.value, BaseExceptionGroup) and raised.value.message == exc.message


@pytest.mark.asyncio
async def test_a_client_that_reads_nothing_does_not_hold_the_rejection_forever(make_app, monkeypatch):
    """N6（變異 fn03）：標頭被背壓卡住時換 token，閘道改送 403；但完全不讀的用戶端連 403 也送不出去。閘道最多等
    ``REJECTION_SEND_TIMEOUT``（0.25 秒）就放棄：應用程式不拋例外、也沒有任何 ``http.response.start`` 交給 transport 就返回。

    這是已知的取捨（docs/mcp-gateway-limits.md）：之後 uvicorn 會記一行 ``ASGI callable returned without starting response``
    （ERROR，沒有 traceback），用戶端恢復讀取時收到純文字的 500。不是乾淨的 403，但比卡住連線、佔著 worker 好。

    不用牆鐘判斷：整個流程等事件，只有最外層的「返回了嗎」有一個寬鬆的上限（遠大於 0.25 秒）；逾時被拿掉或改很大時，應用程式
    永遠不返回，測試就在這個上限逾時。"""
    app, pm, forwarded = _gateway(make_app, monkeypatch)
    messages: asyncio.Queue = asyncio.Queue()
    await messages.put({"type": "http.request", "body": json.dumps(INIT).encode(), "more_body": False})
    stuck, never = asyncio.Event(), asyncio.Event()
    attempts: list[int] = []

    async def receive():
        return await messages.get()

    async def send(event):
        # 外層（uvicorn）完全不動：任何標頭（原本的 200、改送的 403）都送不出去。
        if event["type"] == "http.response.start":
            attempts.append(event["status"])
            stuck.set()
            await never.wait()

    task = asyncio.create_task(app(_scope(), receive, send))
    try:
        await asyncio.wait_for(stuck.wait(), 30)
        assert forwarded == ["initialize"]
        await _revoke()
        await asyncio.wait_for(task, 20)  # 以前（沒有逾時）或逾時太大：永遠不返回
        assert task.exception() is None  # 吞掉 _Rejected，應用程式不拋例外到 uvicorn
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert attempts == [200, 403]  # 先卡在原本的標頭，換 token 後改送 403 也卡住；兩個都沒有真的交給 transport
    assert pm._initialize_dispatches == set()
