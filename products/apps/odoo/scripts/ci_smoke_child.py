"""Built-image XML-RPC safety smoke: loopback fake only, no real Odoo.

CI runs this in a container created from the exact candidate image ID with
``--network none``. The fake records a create before dropping its reply, then
checks that neither a disconnect nor a redirect replays a write. A separate
read demonstrates that OdooClient's explicit read-only retry still works.
"""

from __future__ import annotations

import hashlib
import os
import socket
import threading
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from odoo_mcp import odoo_client
from odoo_mcp_admin import transport_safety


class Backend(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), BackendHandler)
        self.counts: dict[str, int] = {}
        self.redirect_to = ""
        self.lock = threading.Lock()

    def record(self, key: str) -> int:
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]


class BackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        backend: Backend = self.server  # type: ignore[assignment]
        params, rpc = xmlrpc.client.loads(
            self.rfile.read(int(self.headers["Content-Length"]))
        )
        if self.path == "/xmlrpc/2/common" and rpc == "authenticate":
            value = 2
        elif self.path == "/xmlrpc/2/object" and rpc == "execute_kw":
            model, method = params[3:5]
            if model != "res.partner":
                raise AssertionError("unexpected synthetic model")
            if method == "create" and list(params[5]) == [{"name": "Synthetic Drop"}]:
                if backend.record("write_drop") == 1:
                    self.disconnect()
                    return
                value = 9001
            elif method == "create" and list(params[5]) == [
                {"name": "Synthetic Redirect"}
            ]:
                backend.record("write_redirect")
                self.send_response(307)
                # The pinned SDK checks only a lowercase raw-dict key.
                self.send_header("location", backend.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif method == "search_read":
                if backend.record("read") == 1:
                    self.disconnect()
                    return
                value = [{"id": 7, "name": "Synthetic Read"}]
            else:
                raise AssertionError("unexpected synthetic method")
        else:
            raise AssertionError("unexpected synthetic route")
        body = xmlrpc.client.dumps(
            (value,), methodresponse=True, allow_none=True
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def disconnect(self):
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()


class Sink(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), SinkHandler)
        self.hits = 0


class SinkHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.server.hits += 1
        self.rfile.read(int(self.headers["Content-Length"]))
        body = xmlrpc.client.dumps((9001,), methodresponse=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def expect_failure(operation, label: str) -> None:
    try:
        operation()
    except (
        ConnectionError,
        OSError,
        xmlrpc.client.ProtocolError,
        transport_safety.BackendHTTPError,
    ):
        return
    raise AssertionError(f"{label}: request unexpectedly succeeded")


def main() -> None:
    # Fake-only process. Never inherit a proxy that could route loopback calls elsewhere.
    os.environ.pop("HTTP_PROXY", None)
    os.environ["ODOO_MCP_RETRY_ATTEMPTS"] = "1"
    os.environ["ODOO_MCP_RETRY_BACKOFF"] = "0"
    assert (
        hashlib.sha256(Path(odoo_client.__file__).read_bytes()).hexdigest()
        == transport_safety.ODOO_CLIENT_SHA256
    )
    transport_safety.install()
    backend, sink = Backend(), Sink()
    backend.redirect_to = f"http://127.0.0.1:{sink.server_port}/xmlrpc/2/object"
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (backend, sink)
    ]
    for thread in threads:
        thread.start()
    try:
        client = odoo_client.OdooClient(
            f"http://127.0.0.1:{backend.server_port}",
            "synthetic-db",
            "synthetic-user",
            "synthetic-secret",
            timeout=2,
        )
        expect_failure(
            lambda: client._execute(
                "res.partner", "create", {"name": "Synthetic Drop"}
            ),
            "write drop",
        )
        assert backend.counts == {"write_drop": 1}, backend.counts
        assert client._execute("res.partner", "search_read", [], fields=["name"]) == [
            {"id": 7, "name": "Synthetic Read"}
        ]
        assert backend.counts == {"write_drop": 1, "read": 2}, backend.counts
        expect_failure(
            lambda: client._execute(
                "res.partner", "create", {"name": "Synthetic Redirect"}
            ),
            "write redirect",
        )
        assert backend.counts == {"write_drop": 1, "read": 2, "write_redirect": 1}, (
            backend.counts
        )
        assert sink.hits == 0
    finally:
        for server in (backend, sink):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
    print(
        "odoo-image-synthetic-smoke: PASS write_drop=1 read=2 write_redirect=1 redirect_sink=0"
    )


if __name__ == "__main__":
    main()
