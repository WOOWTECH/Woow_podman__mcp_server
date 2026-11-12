"""A recovered gateway must not strand the first key_info on an old socket."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import urllib.error
import urllib.request

import fastmcp

from woow_litellm_mcp_server.server import build_server
from woow_litellm_mcp_server.settings import get_settings


class FakeGateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.available = True
        self.stale = False
        self.first_socket = None
        self.release = Event()
        super().__init__(("127.0.0.1", 0), GatewayHandler)


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # /key/info carries a key in its query string

    def do_GET(self):
        if not self.server.available:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/health":
            body = b'{"status":"ok"}'
        elif self.path.startswith("/key/info?"):
            if self.server.first_socket is None:
                self.server.first_socket = self.connection
            elif self.server.stale and self.connection is self.server.first_socket:
                # A network partition can leave an old TCP connection alive but
                # unresponsive while a fresh health connection succeeds.
                self.server.release.wait(timeout=5)
                return
            body = b'{"key_alias":"isolated","spend":0}'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def test_first_key_info_after_health_recovery_uses_fresh_connection(monkeypatch):
    gateway = FakeGateway()
    thread = Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{gateway.server_port}"
    monkeypatch.setenv("LITELLM_MCP_BASE_URL", base)
    monkeypatch.setenv("LITELLM_MCP_MASTER_KEY", "synthetic-test-only")
    monkeypatch.setenv("LITELLM_MCP_REQUEST_TIMEOUT", "2")
    get_settings.cache_clear()
    try:
        async with fastmcp.Client(build_server()) as client:
            args = {"key": "sk-synthetic-test-only"}
            baseline = await client.call_tool("litellm_key_info", args)
            assert not baseline.is_error

            gateway.available = False
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                assert False, "outage probe unexpectedly succeeded"
            except urllib.error.HTTPError as error:
                assert error.code == 503
            gateway.available = True
            assert urllib.request.urlopen(base + "/health", timeout=1).status == 200
            gateway.stale = True

            first = await client.call_tool("litellm_key_info", args)
            assert not first.is_error
    finally:
        gateway.release.set()
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=2)
        get_settings.cache_clear()
