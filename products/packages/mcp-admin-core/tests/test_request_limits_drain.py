"""超過上限的 MCP 請求：用戶端先把整個內容送完，也一定讀得到 413＋JSON-RPC 錯誤（第二輪驗證 F3，2026-10-26）。

E2 實測：n8n 的 4 MiB 界線（McpRequestLimits）在讀完請求前就回 413；用戶端帶 ``Connection: close``（urllib、
很多反向代理對上游的連線）時，uvicorn 送完回應就關連線，核心看到還沒讀的資料直接回 RST——經 kubectl port-forward
送 8 MiB 的用戶端拿到 ECONNRESET、看不到 413，port-forward 也跟著斷。現在超過上限時先把剩下的內容讀完丟掉
（最多 ``DRAIN_BYTES``，比 Cloudflare 的 100 MB 上限大）再回應，關連線時沒有未讀的資料。

用真的 uvicorn（背景 thread）＋raw socket：先 sendall 完整內容、再讀回應，就是會被 reset 的那種用戶端。
共用的 16 MiB 上限（request_body.read_body，其他產品）本來就會讀到結尾（找 JSON-RPC id），一併驗證。
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn

from mcp_admin_core.request_limits import DRAIN_BYTES, McpRequestLimits, RequestLimitError

MiB = 1024 * 1024
TOKEN = "drain-test-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Gateway:
    """背景 thread 跑真的 uvicorn＋create_app；不起子行程（超過上限的請求不會轉發）。"""

    def __init__(self, app):
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("gateway did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def gateway_app(tmp_config_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app

    def build(limits: McpRequestLimits | None):
        config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        config.update(mcp_auth_token=TOKEN)
        tmp_config_path.write_text(json.dumps(config))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        app = create_app(title="drain test", mcp_request_limits=limits)
        return app

    return build


def _call(size: int) -> bytes:
    """MCP SDK 寫法（id 在結尾）的 tools/call，整個請求剛好 *size* 位元組。"""
    shell = {"method": "tools/call", "params": {"name": "validate_workflow", "arguments": {"padding": ""}},
             "jsonrpc": "2.0", "id": 7}
    empty = len(json.dumps(shell, separators=(",", ":")))
    shell["params"]["arguments"]["padding"] = "x" * (size - empty)
    return json.dumps(shell, separators=(",", ":")).encode()


def send_all_then_read(port: int, body: bytes, *, close: bool = True, chunked: bool = False) -> dict:
    """先把整個請求送完再讀回應（http.client／urllib 的做法）；回傳送了多少、送／收時的錯誤與回應。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    head = [f"POST /private_{TOKEN}/mcp HTTP/1.1", "Host: 127.0.0.1", "Content-Type: application/json",
            "Accept: application/json, text/event-stream"]
    head.append("Transfer-Encoding: chunked" if chunked else f"Content-Length: {len(body)}")
    if close:
        head.append("Connection: close")
    result = {"sent": 0, "send_error": None, "recv_error": None, "response": b""}
    try:
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        step = 256 * 1024
        for start in range(0, len(body), step):
            piece = body[start:start + step]
            sock.sendall(b"%x\r\n%s\r\n" % (len(piece), piece) if chunked else piece)
            result["sent"] += len(piece)
        if chunked:
            sock.sendall(b"0\r\n\r\n")
    except OSError as exc:
        result["send_error"] = type(exc).__name__
    try:
        while True:
            part = sock.recv(65536)
            if not part:
                break
            result["response"] += part
            if b"\r\n\r\n" in result["response"] and not close:
                head_end = result["response"].index(b"\r\n\r\n") + 4
                length = int(next(line.split(b":", 1)[1] for line in result["response"][:head_end].split(b"\r\n")
                                  if line.lower().startswith(b"content-length:")))
                if len(result["response"]) >= head_end + length:
                    break
    except OSError as exc:
        result["recv_error"] = type(exc).__name__
    finally:
        sock.close()
    return result


def _parsed(result: dict) -> tuple[int, dict]:
    raw = result["response"]
    assert raw, f"no response: {result['send_error']=} {result['recv_error']=} sent={result['sent']}"
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, json.loads(body)


N8N_LIMITS = McpRequestLimits(max_bytes=4 * MiB, max_argument_elements=10_000)


@pytest.mark.parametrize("size", [8 * MiB, 40 * MiB])
def test_n8n_limit_client_that_sends_everything_first_reads_the_413(gateway_app, size):
    with _Gateway(gateway_app(N8N_LIMITS)) as gw:
        result = send_all_then_read(gw.port, _call(size))
    assert result["send_error"] is None and result["sent"] == size, result
    status, reply = _parsed(result)
    assert status == 413
    assert reply == {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32602, "message": "MCP request exceeds 4194304 bytes"}}
    assert result["recv_error"] is None


def test_n8n_limit_chunked_body_is_drained_too(gateway_app):
    with _Gateway(gateway_app(N8N_LIMITS)) as gw:
        result = send_all_then_read(gw.port, _call(12 * MiB), chunked=True)
    assert result["send_error"] is None and result["sent"] == 12 * MiB, result
    assert _parsed(result)[0] == 413


def test_413_is_not_sent_before_the_whole_body_has_arrived(gateway_app):
    """修正的核心：宣告超過上限也要等內容送完才回 413（修正前一看到 Content-Length 就回，之後關連線時才會有未讀的資料）。

    keep-alive 連線、只先送一部分：修正前 413 立刻回來；修正後在剩下的內容送到之前不回，送完才讀得到 413。
    """
    with _Gateway(gateway_app(N8N_LIMITS)) as gw:
        sock = socket.create_connection(("127.0.0.1", gw.port), timeout=30)
        try:
            big = _call(6 * MiB)
            sock.sendall((f"POST /private_{TOKEN}/mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {len(big)}\r\n\r\n").encode() + big[:MiB])
            sock.settimeout(1.0)
            with pytest.raises(socket.timeout):
                early = sock.recv(65536)
                raise AssertionError(f"413 sent before the body arrived: {early[:40]!r}")
            sock.settimeout(30)
            sock.sendall(big[MiB:])
            data = sock.recv(65536)
        finally:
            sock.close()
    assert data.startswith(b"HTTP/1.1 413")


def test_keep_alive_connection_stays_usable_after_the_413(gateway_app):
    """回歸保護（修正前也會過，審查確認過）：讀完丟掉的只有這個請求的內容，同一條 keep-alive 連線的下一個請求照常處理。"""
    with _Gateway(gateway_app(N8N_LIMITS)) as gw:
        sock = socket.create_connection(("127.0.0.1", gw.port), timeout=30)
        try:
            big = _call(6 * MiB)
            sock.sendall((f"POST /private_{TOKEN}/mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {len(big)}\r\n\r\n").encode() + big)
            small = b'{"jsonrpc":"2.0","id":9,"method":"ping"}'
            # 同一條連線的下一個請求（沒有子行程：閘道回 503，重點是連線沒被 reset、讀得到第二個回應）
            sock.sendall((f"POST /private_{TOKEN}/mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {len(small)}\r\nConnection: close\r\n\r\n").encode() + small)
            data = b""
            while True:
                part = sock.recv(65536)
                if not part:
                    break
                data += part
        finally:
            sock.close()
    assert data.startswith(b"HTTP/1.1 413")
    assert data.count(b"HTTP/1.1 ") == 2


def test_shared_16mib_limit_also_reads_to_the_end(gateway_app):
    """其他產品（request_body.read_body）：20 MiB 送完也讀得到可讀的拒收（200＋帶 id 的 JSON-RPC 錯誤）。"""
    with _Gateway(gateway_app(None)) as gw:
        result = send_all_then_read(gw.port, _call(20 * MiB))
    assert result["send_error"] is None and result["sent"] == 20 * MiB, result
    status, reply = _parsed(result)
    assert status == 200 and reply["id"] == 7 and reply["error"]["code"] == -32602


async def test_drain_stops_at_the_cap():
    """宣告或實際超過 DRAIN_BYTES 的就不再讀（只可能來自叢集內），立刻拒收。"""
    limits = McpRequestLimits(max_bytes=10, max_argument_elements=10, drain_bytes=64)
    consumed = []

    async def chunks():
        for _ in range(100):
            consumed.append(16)
            yield {"type": "http.request", "body": b"x" * 16, "more_body": True}
        yield {"type": "http.request", "body": b"", "more_body": False}

    stream = chunks()

    async def receive():
        return await stream.__anext__()

    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/"}, receive)
    with pytest.raises(RequestLimitError) as raised:
        await limits.read_body(request)
    assert raised.value.status == 413
    assert sum(consumed) <= 64 + 16  # 超過上限後最多再讀到 DRAIN 的那一塊
    declared = Request({"type": "http", "method": "POST", "headers": [(b"content-length", b"100000")], "path": "/"},
                       receive)
    before = len(consumed)
    with pytest.raises(RequestLimitError):
        await limits.read_body(declared)
    assert len(consumed) == before  # 宣告就超過 DRAIN：一個位元組都不讀
    assert DRAIN_BYTES > 100 * 1000 * 1000  # 經 Cloudflare（100 MB）進來的都讀得完
