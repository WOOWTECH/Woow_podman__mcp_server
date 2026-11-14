#!/usr/bin/env python3
"""Multi-profile checks for a disposable product container. CI only: this creates profiles and
revokes tokens, so it must never run against an installed service (manage.sh smoke is read-only).

usage: profile-smoke.py setup    CONTAINER BASE_URL STATE_FILE
       profile-smoke.py restored CONTAINER BASE_URL STATE_FILE   (SMOKE_ADMIN_PASSWORD in env)
       profile-smoke.py capacity CONTAINER BASE_URL
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

LABEL = "Podman smoke second account"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "podman-profile-smoke", "version": "1"}}}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


class Client:
    def __init__(self, container: str, base: str) -> None:
        self.container, self.base, self.admin = container, base, ""

    def request(self, path: str, payload: dict | None = None, *, method: str | None = None,
                profile: str = "", headers: dict | None = None) -> tuple[int, dict, bytes]:
        sent = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            sent["Content-Type"] = "application/json"
        if self.admin and path.startswith("/api/"):
            sent["Authorization"] = f"Bearer {self.admin}"
        if profile:
            sent["X-MCP-Profile"] = profile
        if path.endswith("/mcp"):
            sent["Accept"] = "application/json, text/event-stream"
        req = urllib.request.Request(self.base + path, body, sent, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = b"" if path.endswith("/mcp") else response.read(2 * 1024 * 1024)
                return response.status, {k.lower(): v for k, v in response.headers.items()}, data
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read(4096)

    def config(self, relative: str = "config.json") -> dict:
        raw = subprocess.check_output(["podman", "exec", self.container, "cat", f"/data/{relative}"], text=True)
        return json.loads(raw)

    def login(self, password: str) -> None:
        status, _, body = self.request("/api/auth/login", {"password": password})
        assert status == 200, ("login", status)
        self.admin = json.loads(body)["token"]

    def initialize(self, token: str, *, wait: int = 60) -> tuple[int, str]:
        """Initialize through the token URL; wait while a just-started child is still coming up."""
        status, headers = 0, {}
        for _ in range(wait):
            status, headers, _ = self.request(f"/private_{token}/mcp", INITIALIZE)
            if status not in (502, 503):
                break
            time.sleep(1)
        return status, headers.get("mcp-session-id", "")

    def token_write(self, action: str, profile: str) -> dict:
        status, headers, _ = self.request("/api/tokens", profile=profile)
        assert status == 200, ("token info", action, status)
        # Management writes need the read revision and an operation id (<epoch ms>.<16-64 url-safe>).
        extra = {"If-Match": headers["etag"],
                 "Idempotency-Key": f"{int(time.time() * 1000)}.{secrets.token_urlsafe(16)}"}
        status, _, body = self.request(f"/api/tokens/{action}", {} if action == "rotate" else None,
                                       method="POST", profile=profile, headers=extra)
        assert status == 200, ("token", action, status)
        return json.loads(body)


def setup(client: Client, state_file: str) -> None:
    root = client.config()
    client.login(root["admin_password"])
    default_token = root["mcp_auth_token"]
    assert default_token, "default profile has no MCP token"
    admin, client.admin = client.admin, ""
    assert client.request("/api/profiles")[0] == 401
    client.admin = admin
    status, _, body = client.request("/api/profiles")
    assert status == 200 and json.loads(body)["profiles"][0]["id"] == "default", ("profiles", status)

    status, _, body = client.request("/api/profiles", {"label": LABEL}, method="POST")
    assert status == 201, ("create profile", status, body[:200])
    profile_id = json.loads(body)["id"]
    assert len(profile_id) == 24 and all(c in "0123456789abcdef" for c in profile_id)
    profile_token = client.config(f"profiles/{profile_id}/config.json")["mcp_auth_token"]
    assert profile_token and profile_token != default_token, "second profile did not get its own token"

    default_status, default_session = client.initialize(default_token)
    assert default_status == 200, ("default initialize", default_status)
    assert client.initialize(profile_token)[0] == 200, "second profile initialize failed"
    if default_session and not default_session.startswith("woow-mcp-setup-"):
        # A live session of the default profile must not reach the second profile's child.
        status = client.request(f"/private_{profile_token}/mcp", TOOLS_LIST,
                                headers={"Mcp-Session-Id": default_session,
                                         "MCP-Protocol-Version": "2025-03-26"})[0]
        assert status == 404, ("cross-profile session", status)

    client.token_write("revoke", profile_id)
    assert client.initialize(profile_token, wait=1)[0] == 403, "revoked profile token still works"
    assert client.initialize(default_token)[0] == 200, "revoking one profile affected the default"
    profile_token = client.token_write("rotate", profile_id)["token"]
    assert client.initialize(profile_token)[0] == 200, "rotated profile token does not work"

    with open(state_file, "w", encoding="utf-8") as stream:
        json.dump({"default_token": default_token, "profile_id": profile_id,
                   "profile_token": profile_token}, stream)
    print(f"{client.container}: second profile isolation, revoke and rotate passed")


def restored(client: Client, state_file: str) -> None:
    with open(state_file, encoding="utf-8") as stream:
        old = json.load(stream)
    password = os.environ["SMOKE_ADMIN_PASSWORD"]
    root = client.config()
    assert root["admin_password"] == password, "restored GUI password is not the installed secret"
    assert root.get("mcp_auth_revoked") is True and not root.get("mcp_auth_token"), "default token not revoked"
    profile = client.config(f"profiles/{old['profile_id']}/config.json")
    assert profile.get("mcp_auth_revoked") is True and not profile.get("mcp_auth_token"), "profile token not revoked"
    for token in (old["default_token"], old["profile_token"]):
        assert client.initialize(token, wait=1)[0] == 403, "a token from the backup still works"
    client.login(password)
    status, _, body = client.request("/api/profiles")
    assert status == 200 and old["profile_id"] in {p["id"] for p in json.loads(body)["profiles"]}
    for profile_id in ("default", old["profile_id"]):
        status, _, body = client.request("/api/tokens", profile=profile_id)
        assert status == 200 and json.loads(body)["state"] == "revoked", ("token state", profile_id, status)
    fresh = client.token_write("rotate", "default")["token"]
    assert client.initialize(fresh)[0] == 200, "a token issued after the restore does not work"
    print(f"{client.container}: restore revoked every token, kept profiles and accepted the secret password")


def capacity(client: Client) -> None:
    client.login(client.config()["admin_password"])
    status, _, body = client.request("/api/profiles", {"label": LABEL}, method="POST")
    assert status == 409, ("capacity", status)
    assert json.loads(body)["detail"]["code"] == "profile_memory_insufficient", body[:200]
    print(f"{client.container}: memory ceiling refused an extra profile")


def main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[0] not in ("setup", "restored", "capacity"):
        raise SystemExit(__doc__)
    client = Client(argv[1], argv[2])
    if argv[0] == "capacity":
        capacity(client)
    elif argv[0] == "setup":
        setup(client, argv[3])
    else:
        restored(client, argv[3])


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        detail = exc if isinstance(exc, AssertionError) else ""
        print(f"profile smoke failed: {type(exc).__name__}: {detail}", file=sys.stderr)
        raise SystemExit(1) from None
