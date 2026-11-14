#!/usr/bin/env python3
"""Read-only local acceptance of a running Quadlet MCP administration service."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


def request(base: str, path: str, payload: dict | None = None, *, bearer: str = "") -> tuple[int, bytes]:
    headers = {}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if path.endswith("/mcp"):
        headers["Accept"] = "application/json, text/event-stream"
    req = urllib.request.Request(base + path, body, headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, b"" if path.endswith("/mcp") else response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: smoke.py CONTAINER BASE_URL")
    container, base = sys.argv[1:]
    state = json.loads(subprocess.check_output(
        ["podman", "exec", container, "cat", "/data/config.json"], text=True))
    password = state["admin_password"]
    token = state.get("mcp_auth_token", "")
    # A restore revokes every MCP token on purpose; the operator issues new ones in the GUI.
    revoked = state.get("mcp_auth_revoked") is True and not token
    if not password or not (token or revoked):
        raise RuntimeError("first-boot credentials or MCP token are missing")
    status, body = request(base, "/healthz")
    assert status == 200, ("healthz", status)
    health = json.loads(body)
    assert health["status"] == "ok"
    status, html = request(base, "/")
    assert status == 200 and b"<html" in html.lower(), ("GUI", status)
    asset = re.search(rb'(?:src|href)="(/assets/[^" ]+)"', html)
    if asset:
        assert request(base, asset.group(1).decode())[0] == 200
    status, body = request(base, "/api/auth/login", {"password": password})
    assert status == 200, ("login", status)
    admin_token = json.loads(body)["token"]
    assert request(base, "/api/auth/login", {"password": "invalid-smoke-password"})[0] == 401
    assert request(base, "/api/capabilities", bearer=admin_token)[0] == 200
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "quadlet-smoke", "version": "1"}}}
    assert request(base, "/private_invalid-smoke-token/mcp", initialize)[0] == 403
    if revoked:
        print(f"{container}: MCP token is revoked (restored backup); issue a new one in the GUI")
    elif health.get("mcp") == "running":
        for _ in range(15):
            if request(base, f"/private_{token}/mcp", initialize)[0] == 200:
                break
            time.sleep(1)
        else:
            raise RuntimeError("MCP child did not accept initialize")
        for _ in range(15):
            status, body = request(base, "/api/capabilities", bearer=admin_token)
            tool_status = json.loads(body)["tools"]["status"] if status == 200 else "unavailable"
            if tool_status == "observed":
                break
            time.sleep(1)
        else:
            if health.get("backend") != "missing" or tool_status != "unknown":
                raise RuntimeError("MCP child is running but tool discovery failed")
            print(f"{container}: MCP initialize passed; tool discovery needs backend configuration")
    print(f"{container}: GUI, login, token and MCP state {health.get('mcp', 'unknown')} passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"smoke failed: {type(exc).__name__}: {exc if isinstance(exc, (AssertionError, RuntimeError)) else ''}", file=sys.stderr)
        raise SystemExit(1) from None
