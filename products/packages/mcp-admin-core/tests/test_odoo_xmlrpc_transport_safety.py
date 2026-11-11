"""The Odoo XML-RPC transport must not replay an ambiguous write.

Only a loopback fake is used. Its ``create`` branch records the write in memory
before closing the connection, so an implicit transport retry is observable.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("odoo_mcp")
pytest.importorskip("odoo_mcp_admin")

import _odoo_child


class _SyntheticOdoo(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode: str):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = mode
        self.lock = threading.Lock()
        self.attempts: dict[str, int] = {}
        self.unexpected: list[str] = []
        self.redirect_to = ""

    def record(self, method: str) -> int:
        with self.lock:
            self.attempts[method] = self.attempts.get(method, 0) + 1
            return self.attempts[method]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        server: _SyntheticOdoo = self.server  # type: ignore[assignment]
        params, rpc_method = xmlrpc.client.loads(
            self.rfile.read(int(self.headers["Content-Length"]))
        )
        if self.path == "/xmlrpc/2/common" and rpc_method == "authenticate":
            value = 2
        elif self.path == "/xmlrpc/2/object" and rpc_method == "execute_kw":
            model, method = params[3:5]
            if (
                method == "create"
                and model == "res.partner"
                and list(params[5]) == [{"name": "Synthetic Write Control"}]
            ):
                attempt = server.record(
                    "create"
                )  # committed in memory before the connection is lost
                if server.mode == "write_drop" and attempt == 1:
                    self._disconnect()
                    return
                if server.mode == "write_redirect" and attempt == 1:
                    self.send_response(307)
                    # Pinned RedirectTransport reads a raw dict with case-sensitive
                    # .get('location'); titlecase would make this negative vacuous.
                    self.send_header("location", server.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                value = 9001
            elif method == "search_read" and model == "res.partner":
                attempt = server.record("search_read")
                if server.mode == "read_redirect":
                    self.send_response(307)
                    self.send_header("location", server.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if server.mode == "read_recover" and attempt == 1:
                    self._disconnect()
                    return
                value = [{"id": 7, "name": "Synthetic Read Control"}]
            elif method == "fields_get":
                value = {"name": {"type": "char", "string": "Name", "store": True}}
            else:
                with server.lock:
                    server.unexpected.append(f"{model}.{method}")
                value = True
        else:
            with server.lock:
                server.unexpected.append(f"{self.path}:{rpc_method}")
            value = True
        body = xmlrpc.client.dumps(
            (value,), methodresponse=True, allow_none=True
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _disconnect(self):
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()


class _Sink(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _SinkHandler)
        self.hits = 0
        self.synthetic_secret_seen = False


class _SinkHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.server.hits += 1
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.synthetic_secret_seen |= b"SYNTHETIC-SECRET-NEVER-LEAK" in body
        body = xmlrpc.client.dumps((9001,), methodresponse=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def redirect_sink():
    sink = _Sink()
    thread = threading.Thread(target=sink.serve_forever, daemon=True)
    thread.start()
    try:
        yield sink
    finally:
        sink.shutdown()
        sink.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("mode", ["write_drop", "write_redirect"])
def test_committed_synthetic_write_is_never_replayed(
    mode, monkeypatch, tmp_path, redirect_sink
):
    """A lost reply or redirect must not turn one MCP create into two writes."""
    server = _SyntheticOdoo(mode)
    server.redirect_to = f"http://127.0.0.1:{redirect_sink.server_port}/xmlrpc/2/object"
    monkeypatch.setattr(_odoo_child, "FakeOdoo", lambda: server)
    site = _odoo_child.build_patched_site(tmp_path / "site")

    async def scenario(call_tool, _rpc):
        return await call_tool(
            "execute_method",
            {
                "model": "res.partner",
                "method": "create",
                "args": [{"name": "Synthetic Write Control"}],
            },
        )

    out = _odoo_child.run_child(
        site,
        {
            "ODOO_MCP_ENABLE_WRITES": "1",
            "ODOO_MCP_ALLOW_DIRECT_WRITES": "1",
            "ODOO_MCP_RETRY_ATTEMPTS": "0",
            "ODOO_PASSWORD": "SYNTHETIC-SECRET-NEVER-LEAK",
        },
        tmp_path,
        scenario,
    )
    assert (redirect_sink.hits, redirect_sink.synthetic_secret_seen) == (0, False)
    assert out["result"]["isError"] is True
    assert server.attempts == {"create": 1}
    assert server.unexpected == []
    assert "SYNTHETIC-SECRET-NEVER-LEAK" not in str(out["result"])
    assert server.redirect_to not in str(out["result"])


def test_read_redirect_does_not_forward_credentials(
    monkeypatch, tmp_path, redirect_sink
):
    """No reviewed redirect allowlist exists, even for otherwise retryable reads."""
    server = _SyntheticOdoo("read_redirect")
    server.redirect_to = f"http://127.0.0.1:{redirect_sink.server_port}/xmlrpc/2/object"
    monkeypatch.setattr(_odoo_child, "FakeOdoo", lambda: server)
    site = _odoo_child.build_patched_site(tmp_path / "site")

    async def scenario(call_tool, _rpc):
        return await call_tool(
            "search_records", {"model": "res.partner", "fields": ["name"], "limit": 1}
        )

    out = _odoo_child.run_child(
        site,
        {
            "ODOO_MCP_RETRY_ATTEMPTS": "0",
            "ODOO_PASSWORD": "SYNTHETIC-SECRET-NEVER-LEAK",
        },
        tmp_path,
        scenario,
    )
    assert (redirect_sink.hits, redirect_sink.synthetic_secret_seen) == (0, False)
    assert out["result"]["isError"] is True
    assert server.attempts == {"search_read": 1}
    assert server.unexpected == []
    assert "SYNTHETIC-SECRET-NEVER-LEAK" not in str(out["result"])
    assert server.redirect_to not in str(out["result"])


def test_only_odoo_client_retries_transient_read(monkeypatch, tmp_path):
    """One safe retry occurs in OdooClient._execute, not the transport."""
    server = _SyntheticOdoo("read_recover")
    monkeypatch.setattr(_odoo_child, "FakeOdoo", lambda: server)
    site = _odoo_child.build_patched_site(tmp_path / "site")

    async def scenario(call_tool, _rpc):
        return await call_tool(
            "search_records", {"model": "res.partner", "fields": ["name"], "limit": 1}
        )

    out = _odoo_child.run_child(
        site,
        {"ODOO_MCP_RETRY_ATTEMPTS": "1", "ODOO_MCP_RETRY_BACKOFF": "0"},
        tmp_path,
        scenario,
    )
    assert out["result"].get("isError") is not True
    assert _odoo_child.payload(out["result"])["success"] is True
    assert server.attempts == {"search_read": 2}
    assert server.unexpected == []
    assert "Retrying res.partner.search_read after connection error" in out["stderr"]


def test_unknown_odoo_client_source_fails_closed(monkeypatch, tmp_path):
    from odoo_mcp import odoo_client
    from odoo_mcp_admin import transport_safety

    changed = tmp_path / "odoo_client.py"
    changed.write_text("# unreviewed wheel\n")
    monkeypatch.setattr(odoo_client, "__file__", str(changed))
    with pytest.raises(RuntimeError, match="source review"):
        transport_safety.install()


def test_pinned_package_import_does_not_construct_odoo_client():
    """transport_safety imports the upstream package before install; import must be inert."""
    code = """
import sys
constructed = []
def observe(frame, event, _arg):
    if (event == 'call' and frame.f_globals.get('__name__') == 'odoo_mcp.odoo_client'
            and frame.f_code.co_name == '__init__'
            and type(frame.f_locals.get('self')).__name__ == 'OdooClient'):
        constructed.append(1)
sys.setprofile(observe)
from odoo_mcp_admin import launcher
sys.setprofile(None)
assert constructed == [], 'upstream import constructed an OdooClient before safety install'
from odoo_mcp_admin import transport_safety
transport_safety.install()
assert transport_safety.client.RedirectTransport.request is transport_safety._request_once
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]


class _Raising:
    def __init__(self, exc):
        self.exc = exc

    def single_request(self, *_args):
        raise self.exc


@pytest.mark.parametrize(
    ("raised", "kind", "text"),
    [
        (socket.timeout("timed out"), TimeoutError, "BACKEND_TIMEOUT: timed out"),
        (
            ConnectionResetError("Remote end closed connection without response"),
            ConnectionError,
            "BACKEND_UNAVAILABLE: Remote end closed connection without response",
        ),
        (OSError("[Errno 111] Connection refused"), ConnectionError,
         "BACKEND_UNAVAILABLE: [Errno 111] Connection refused"),
        (
            xmlrpc.client.ProtocolError(
                "http://odoo.internal/xmlrpc/2/object", 500, "BACKEND-CONTROLLED-TEXT", {}
            ),
            ValueError,
            "BACKEND_HTTP_ERROR status=500: Internal Server Error",
        ),
        (
            xmlrpc.client.ProtocolError(
                "http://odoo.internal/x", 302, "Found", {"location": "http://secret.internal/"}
            ),
            ValueError,
            "BACKEND_HTTP_ERROR status=302: Found",
        ),
        (ValueError("not well-formed"), ValueError, "BACKEND_UNAVAILABLE: not well-formed"),
    ],
)
def test_transport_errors_use_the_ha_public_code_prefix(raised, kind, text):
    from odoo_mcp_admin import transport_safety

    with pytest.raises(kind) as caught:
        transport_safety._request_once(_Raising(raised), "h", "/p", b"")
    assert str(caught.value) == text
    for leaked in ("odoo.internal", "secret.internal", "BACKEND-CONTROLLED-TEXT"):
        assert leaked not in str(caught.value)


def test_odoo_fault_passes_through_unchanged():
    from odoo_mcp_admin import transport_safety

    fault = xmlrpc.client.Fault(2, "AccessError")
    with pytest.raises(xmlrpc.client.Fault) as caught:
        transport_safety._request_once(_Raising(fault), "h", "/p", b"")
    assert caught.value is fault


def test_http_status_read_is_not_retried_but_a_drop_still_is(monkeypatch, tmp_path):
    """HA parity: a 500 on a read is one backend hit; the retry stays for connection drops and timeouts."""
    from odoo_mcp import odoo_client
    from odoo_mcp_admin import transport_safety

    transport_safety.install()
    hits = []

    class Fake(odoo_client.OdooClient):
        def __init__(self):  # no network, no authenticate
            self.transport = "xmlrpc"
            self.db = self.uid = self.password = None

        def _execute_once(self, model, method, args, kwargs):
            hits.append(method)
            raise outcome.pop(0)

    monkeypatch.setenv("ODOO_MCP_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("ODOO_MCP_RETRY_BACKOFF", "0")
    client = Fake()
    client._apply_lang_context = lambda kw: kw

    outcome = [transport_safety.BackendHTTPError(500)]
    with pytest.raises(ValueError, match="BACKEND_HTTP_ERROR status=500: Internal Server Error"):
        client._execute("res.partner", "search_read", [])
    assert hits == ["search_read"]  # one attempt on a read

    hits.clear()
    outcome = [ConnectionError("BACKEND_UNAVAILABLE: x")] * 3
    with pytest.raises(ConnectionError):
        client._execute("res.partner", "search_read", [])
    assert len(hits) == 3  # drop: 1 + 2 retries

    hits.clear()
    outcome = [TimeoutError("BACKEND_TIMEOUT: timed out")] * 3
    with pytest.raises(TimeoutError):
        client._execute("res.partner", "search_read", [])
    assert len(hits) == 3

    hits.clear()
    outcome = [ConnectionError("BACKEND_UNAVAILABLE: x")] * 3
    with pytest.raises(ConnectionError):
        client._execute("res.partner", "create", [{}])
    assert len(hits) == 1  # writes: single attempt
