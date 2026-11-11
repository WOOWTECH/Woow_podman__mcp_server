"""In-image OpenDesign child/MCP-wire smoke against an owned loopback daemon.

Run with ``docker exec -i <container> python - < this_file``. No real backend,
credentials, registry or cluster access is needed. The fake daemon records every
request so metadata reads cannot quietly become writes.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

SECRET = "CI_SYNTHETIC_SECRET_MARKER"
INTERNAL_URL = "http://synthetic-internal.invalid/private"
PRIVATE_PATH = "/synthetic/private/path"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-03-26",
}
TOOL_PATHS = {"list_connectors": "/api/connectors", "list_runs": "/api/runs"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def require_loopback_listener(port: int) -> None:
    """Check the bound address, not just whether localhost can connect."""
    listeners = []
    for name in ("tcp", "tcp6"):
        path = Path(f"/proc/net/{name}")
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            columns = line.split()
            address, _, port_hex = columns[1].rpartition(":")
            if columns[3] == "0A" and port_hex == f"{port:04X}":
                listeners.append((name, address))
    require(
        listeners == [("tcp", "0100007F")],
        f"child listener is not loopback-only: {listeners}",
    )


def call_mcp(
    client: httpx.Client, url: str, method: str, params: dict, request_id: int
) -> tuple[dict, str]:
    response = client.post(
        url,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        headers=MCP_HEADERS,
    )
    require(
        response.status_code == 200,
        f"MCP {method} returned HTTP {response.status_code}",
    )
    messages = [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    message = messages[-1] if messages else response.json()
    require(
        message.get("id") == request_id and isinstance(message.get("result"), dict),
        f"MCP {method} did not return the expected result",
    )
    return message["result"], response.text


def tool_call(
    client: httpx.Client, url: str, name: str, request_id: int
) -> tuple[dict, str]:
    return call_mcp(
        client, url, "tools/call", {"name": name, "arguments": {}}, request_id
    )


def expected_success(name: str) -> dict:
    if name == "list_connectors":
        return {
            "total": 1,
            "connectors": [
                {
                    "id": "demo",
                    "name": "Demo connector",
                    "provider": "demo",
                    "category": "external",
                    "status": "ready",
                    "configured": True,
                    "toolCount": 1,
                    "tools": ["list_records"],
                }
            ],
            "truncated": False,
        }
    return {
        "runs": [{"id": "run-1", "projectId": "project-1", "status": "running"}],
        "truncated": False,
    }


def run_smoke() -> dict:
    events: list[tuple[str, str]] = []
    state = {"mode": "success"}
    lock = threading.Lock()

    class FakeDaemon(BaseHTTPRequestHandler):
        # Force a fresh connection for refusal after shutdown.
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args: object) -> None:
            pass

        def _reply(
            self, status: int, body: bytes, content_type: str = "application/json"
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            with lock:
                events.append((self.command, self.path))
                mode = state["mode"]
            if self.command != "GET":
                self._reply(405, b"{}")
            elif self.path not in TOOL_PATHS.values():
                self._reply(404, b"{}")
            elif mode == "http_error":
                self._reply(503, f"daemon error {SECRET}".encode(), "text/plain")
            elif mode == "invalid":
                self._reply(200, f'{{"secret":"{SECRET}","data":'.encode())
            elif self.path == "/api/connectors":
                self._reply(
                    200,
                    json.dumps(
                        {
                            "connectors": [
                                {
                                    "id": "demo",
                                    "name": "Demo\u202e connector",
                                    "provider": "demo",
                                    "category": "external",
                                    "status": "ready",
                                    "auth": {"configured": True, "token": SECRET},
                                    "url": INTERNAL_URL,
                                    "cwd": PRIVATE_PATH,
                                    "tools": [
                                        {"name": "list_records", "description": SECRET}
                                    ],
                                }
                            ]
                        }
                    ).encode(),
                )
            else:
                self._reply(
                    200,
                    json.dumps(
                        {
                            "runs": [
                                {
                                    "id": "run-1",
                                    "projectId": "project-1",
                                    "status": "running",
                                    "error": SECRET,
                                    "cwd": PRIVATE_PATH,
                                    "url": INTERNAL_URL,
                                }
                            ]
                        }
                    ).encode(),
                )

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle

    daemon = ThreadingHTTPServer(("127.0.0.1", 0), FakeDaemon)
    daemon.daemon_threads = True
    thread = threading.Thread(
        target=daemon.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    daemon_running = True
    child: subprocess.Popen | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="opendesign-child-smoke-") as directory:
            base = f"http://127.0.0.1:{daemon.server_port}"
            port = free_port()
            # Deliberately omit inherited OD credentials, proxy variables and real backend URLs.
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": directory,
                "TMPDIR": directory,
                "OD_API_BASE": base,
                "NO_PROXY": "127.0.0.1,localhost",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            if pythonpath := os.environ.get("PYTHONPATH"):
                env["PYTHONPATH"] = pythonpath
            with (Path(directory) / "child.log").open("w+") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "opendesign_mcp_server.launcher",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                for _ in range(100):
                    require(
                        child.poll() is None, "OpenDesign child exited during startup"
                    )
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("OpenDesign child startup timed out")
                require_loopback_listener(port)

                wire: list[str] = []
                mcp_url = f"http://127.0.0.1:{port}/mcp"
                with httpx.Client(timeout=5.0, trust_env=False) as client:
                    listed, raw = call_mcp(client, mcp_url, "tools/list", {}, 1)
                    wire.append(raw)
                    names = {tool["name"] for tool in listed.get("tools", [])}
                    require(
                        TOOL_PATHS.keys() <= names,
                        "metadata tools missing from child MCP registry",
                    )
                    request_id = 1
                    for mode, code in (
                        ("success", None),
                        ("http_error", "BACKEND_HTTP_ERROR status=503"),
                        ("invalid", "BACKEND_INVALID_RESPONSE"),
                    ):
                        with lock:
                            state["mode"] = mode
                        for name in TOOL_PATHS:
                            request_id += 1
                            result, raw = tool_call(client, mcp_url, name, request_id)
                            wire.append(raw)
                            if code is None:
                                require(
                                    result.get("isError") is False,
                                    f"{name} success became MCP error",
                                )
                                actual = json.loads(result["content"][0]["text"])
                                require(
                                    actual == expected_success(name),
                                    f"{name} projection differs",
                                )
                            else:
                                error = result["content"][0]["text"]
                                require(
                                    result.get("isError") is True
                                    and error.endswith(code),
                                    f"{name} did not return fixed {mode} code",
                                )

                    # Stop the only backend, then test genuine loopback connection refusal.
                    daemon.shutdown()
                    daemon.server_close()
                    thread.join(timeout=2)
                    daemon_running = False
                    require(not thread.is_alive(), "fake daemon did not stop")
                    for name in TOOL_PATHS:
                        request_id += 1
                        result, raw = tool_call(client, mcp_url, name, request_id)
                        wire.append(raw)
                        error = result["content"][0]["text"]
                        require(
                            result.get("isError") is True
                            and error.endswith("BACKEND_UNAVAILABLE"),
                            f"{name} did not map connection refusal to BACKEND_UNAVAILABLE",
                        )

                expected_events = [
                    ("GET", path) for _ in range(3) for path in TOOL_PATHS.values()
                ]
                require(
                    events == expected_events,
                    f"fake daemon saw unexpected method/path: {events}",
                )
                require(
                    all(
                        marker not in raw
                        for raw in wire
                        for marker in (SECRET, INTERNAL_URL, PRIVATE_PATH, base)
                    ),
                    "MCP wire leaked fake daemon data or URL",
                )
                child.terminate()
                child.wait(timeout=5)
                require(
                    child.returncode in (0, -signal.SIGTERM),
                    "OpenDesign child did not stop cleanly",
                )
                child = None
                log.seek(0)
                require(
                    SECRET not in log.read(),
                    "OpenDesign child log leaked synthetic secret",
                )
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if daemon_running:
            daemon.shutdown()
            daemon.server_close()
            thread.join(timeout=2)
    return {
        "status": "PASS",
        "fixture": "loopback-fake-daemon",
        "tool_calls": request_id - 1,
        "backend_gets": sum(method == "GET" for method, _ in events),
        "backend_writes": sum(method != "GET" for method, _ in events),
        "mcp_listener": "127.0.0.1",
        "real_backend": False,
    }


if __name__ == "__main__":
    print(json.dumps(run_smoke(), sort_keys=True))
