"""First boot for every PaaS sourced MCP admin image run under Quadlet."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

import uvicorn


def seed_config(path: Path, password: str, app_module: str) -> bool:
    """Create a private initial config once; never overwrite persisted state."""
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        return False
    if not password or password == "admin" or len(password) < 12:
        raise ValueError("admin password must be a unique value of at least 12 characters")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-seed-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            config = {"admin_password": password}
            if app_module == "n8n_mcp_admin.main:app":
                # The bundled n8n-mcp child requires a separate upstream bearer.
                # The local proxy must send this exact value to the child.
                upstream_token = secrets.token_urlsafe(32)
                config["mcp_server"] = {"port": 3000, "env": {"AUTH_TOKEN": upstream_token}}
                config["proxy"] = {"bearer_token": upstream_token}
            json.dump(config, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        os.unlink(temporary)


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].endswith(":app"):
        raise SystemExit("usage: bootstrap.py package.main:app")
    path = Path(os.environ.get("MCP_ADMIN_CONFIG", "/data/config.json"))
    password = os.environ.pop("ADMIN_PASSWORD", "")
    seed_config(path, password, sys.argv[1])
    uvicorn.run(sys.argv[1], host="0.0.0.0", port=8080, access_log=False)


if __name__ == "__main__":
    main()
