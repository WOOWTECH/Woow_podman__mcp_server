#!/usr/bin/env python3
"""Exercise the built image through first boot, GUI setup and a live MCP child."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


CONTAINER, PORT = sys.argv[1:3]
BASE = f"http://127.0.0.1:{PORT}"


def request(path: str, payload: dict | None = None, *, origin: str = "",
            bearer: str = "", etag: str = "", method: str | None = None):
    headers = {}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    if origin:
        headers["Origin"] = origin
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if etag:
        headers["If-Match"] = etag
    if path.endswith("/mcp"):
        headers["Accept"] = "application/json, text/event-stream"
    req = urllib.request.Request(BASE + path, body, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.headers, (b"" if path.endswith("/mcp") else response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def state() -> dict:
    if os.environ.get("HEADSCALE_SMOKE_CONFIG"):
        with open(os.environ["HEADSCALE_SMOKE_CONFIG"], encoding="utf-8") as stream:
            return json.load(stream)
    raw = subprocess.check_output(
        ["podman", "exec", CONTAINER, "cat", "/data/config.json"], text=True
    )
    return json.loads(raw)


def main() -> None:
    for _ in range(90):
        try:
            if request("/healthz")[0] == 200:
                break
        except (OSError, TimeoutError):
            pass
        time.sleep(2)
    else:
        raise RuntimeError("Headscale container did not become healthy")

    config = state()
    password = config["admin_password"]
    token = config["mcp_auth_token"]
    assert password and len(password) >= 12
    assert token
    assert request("/")[0] == 200
    assert request("/assets/app.js")[0] == 200
    login_status, _, login_body = request("/api/auth/login", {"password": password}, origin=BASE)
    assert login_status == 200
    assert request("/api/auth/login", {"password": "wrong-ci-password"}, origin=BASE)[0] == 401
    admin_token = json.loads(login_body)["token"]

    status, headers, response_body = request("/api/headscale/connection", bearer=admin_token)
    assert status == 200
    connection = json.loads(response_body)
    if not connection["credentials"]["api_key"]["set"]:
        status, _, _ = request("/api/headscale/connection", {
            "base_url": "https://headscale.example.com",
            "api_key": {"action": "replace", "value": "ci-headscale-api-key"},
        }, bearer=admin_token, origin=BASE, etag=headers["ETag"], method="PUT")
        assert status == 200

    for _ in range(30):
        health_status, _, health_body = request("/healthz")
        if health_status == 200 and json.loads(health_body).get("mcp") == "running":
            break
        time.sleep(1)
    else:
        raise RuntimeError("MCP child did not start after GUI connection setup")

    for _ in range(30):
        status, _, catalog_body = request("/api/capabilities", bearer=admin_token)
        assert status == 200
        catalog = json.loads(catalog_body)
        if catalog["tools"]["status"] == "observed":
            break
        time.sleep(1)
    else:
        raise RuntimeError("Live MCP tool discovery did not succeed")
    assert catalog["deployment"]["kind"] == "podman"
    assert catalog["health"]["child"] == "running"
    assert catalog["tools"]["status"] == "observed"
    # The GUI exposes 30 task actions: 29 native APIs plus local policy diff.
    assert sum(len(item["operations"]) for item in catalog["tools"]["items"]
               if item["name"] != "headscale_platform") == 30

    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "headscale-podman-ci", "version": "1"},
    }}
    assert request(f"/private_{token}/mcp", initialize)[0] == 200
    assert request("/private_invalid-ci-token/mcp", initialize)[0] == 403
    assert request(f"/private_{token}/mcp", initialize, origin="https://untrusted.example")[0] == 403
    print("Headscale first boot, GUI setup, 29 native operations, MCP and Origin checks passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Container smoke failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
