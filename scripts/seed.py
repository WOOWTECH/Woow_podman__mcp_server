#!/usr/bin/env python3
"""Seed /data/config.json for a first boot without touching the GUI.

Idempotent by default: an existing file is left alone unless ``--force`` is
given, because blowing away a live config would rotate the admin password and
the connector token in one step.

    python3 scripts/seed.py --config ./data/config.json \\
        --podman-uri unix:///run/podman/podman.sock \\
        --profile safe

The MCP auth token it mints is printed once.  It is the whole of the
authentication on ``/private_<token>/mcp/``, so treat the output like a
password: it is not recoverable from the file afterwards in plaintext form
anywhere else, and rotating it from the Tokens page invalidates it.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

# The command the admin console spawns.  ``-u`` matters: without unbuffered
# stdio the child's startup lines sit in a pipe buffer and the Logs page looks
# frozen for the first minute.
DEFAULT_COMMAND = sys.executable or "python3"
DEFAULT_ARGS = ["-u", "-m", "woow_podman_mcp_server.server", "--transport", "streamable-http"]

# Loopback only.  The child must never be reachable except through the admin
# console's authenticated reverse proxy.
DEFAULT_MCP_PORT = 8000


def build_config(args: argparse.Namespace) -> dict:
    admin_password = args.admin_password or secrets.token_urlsafe(16)
    mcp_auth_token = args.mcp_auth_token or secrets.token_urlsafe(32)

    connection = {
        "podman_uri": args.podman_uri,
        "podman_api_version": args.podman_api_version,
    }
    for key, value in (
        ("podman_tls_ca", args.podman_tls_ca),
        ("podman_tls_cert", args.podman_tls_cert),
        ("podman_tls_key", args.podman_tls_key),
    ):
        if value:
            connection[key] = value

    return {
        "admin_password": admin_password,
        "mcp_auth_token": mcp_auth_token,
        "connection": connection,
        "tools": {
            "readonly": False,
            "disabled_categories": [],
            "disabled_tools": [],
            "disabled_operations": {},
            "permissions": {"allowed_tools": ["*"], "denied_tools": []},
        },
        "mcp_server": {
            "command": DEFAULT_COMMAND,
            "args": [
                *DEFAULT_ARGS,
                "--host",
                "127.0.0.1",
                "--port",
                str(args.mcp_port),
                "--path",
                "/mcp",
            ],
            "port": args.mcp_port,
            "env": {
                "PODMAN_MCP_PROFILE": args.profile,
                "PODMAN_MCP_MAX_CHARS": str(args.max_chars),
            },
        },
        "proxy": {"timeout": 86400},
        "token_history": [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("MCP_ADMIN_CONFIG", "/data/config.json"))
    ap.add_argument("--podman-uri", default="unix:///run/podman/podman.sock")
    ap.add_argument("--podman-api-version", default="v5.0.0")
    ap.add_argument("--podman-tls-ca", default="")
    ap.add_argument("--podman-tls-cert", default="")
    ap.add_argument("--podman-tls-key", default="")
    ap.add_argument("--profile", choices=("readonly", "safe", "full"), default="safe")
    ap.add_argument("--max-chars", type=int, default=20000)
    ap.add_argument("--mcp-port", type=int, default=DEFAULT_MCP_PORT)
    ap.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD", ""))
    ap.add_argument("--mcp-auth-token", default="")
    ap.add_argument("--force", action="store_true", help="overwrite an existing config file")
    args = ap.parse_args()

    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"{path} already exists — refusing to overwrite (use --force).", file=sys.stderr)
        return 1

    config = build_config(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same atomic write-then-rename the runtime store uses, for the same reason.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    print(f"Wrote {path}")
    print(f"  admin password : {config['admin_password']}")
    print(f"  MCP token      : {config['mcp_auth_token']}")
    print(f"  connector URL  : http://<host>:8080/private_{config['mcp_auth_token']}/mcp/")
    print("Save these now — the file stores them in plaintext but the console masks every read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
