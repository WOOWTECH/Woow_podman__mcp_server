"""CI smoke（在已建好的 nextcloud-mcp-admin 映像容器內執行）：子行程 launcher 能起來、只綁 127.0.0.1、
新安裝的政策（停用清單空）9 支全開、停用 4 支寫入／刪除工具時只剩 5 支讀取。不連任何 Nextcloud（列工具不需要後端），
帳密為隨機假值。

用法（CI）：docker exec -i smoke-nextcloud python - < apps/nextcloud/scripts/ci_smoke_child.py
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time

from fastmcp import Client

READ = ["get_file_tree", "get_file_content", "read_text_file", "list_calendars", "list_tasks"]
WRITE_DELETE = ["create_text_file", "update_text_file", "upload_file", "delete_file_checked"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def list_tools(url: str, timeout: float = 30.0) -> list[str]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            async with Client(url) as client:
                return [t.name for t in await client.list_tools()]
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(0.3)
    raise SystemExit(f"child did not answer on {url}: {last!r}")


def run(disabled: list[str], expected: list[str]) -> None:
    port = free_port()
    password = secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"tools": {"disabled": disabled}}, fh)
        env = {
            **os.environ,
            "MCP_ADMIN_CONFIG": cfg,
            "NEXTCLOUD_MCP_BASE_URL": "https://nextcloud.invalid",
            "NEXTCLOUD_MCP_USERNAME": "ci-smoke",
            "NEXTCLOUD_MCP_APP_PASSWORD": password,
        }
        proc = subprocess.Popen(
            ["nextcloud-mcp-server", "--port", str(port), "--host", "0.0.0.0"],
            env=env, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            names = asyncio.run(list_tools(f"http://127.0.0.1:{port}/mcp"))
            listening = subprocess.run(["sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"],
                                       capture_output=True, text=True).stdout
        finally:
            proc.terminate()
            try:
                output, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate()
    print(f"policy disabled={disabled} -> tools={names}")
    assert names == expected, (names, expected)
    assert password not in output, "app password leaked into child output"
    port_hex = f"{port:04X}"
    bound = [line.split()[1] for line in listening.splitlines()[1:] if line.split()[1].endswith(f":{port_hex}")]
    # 0100007F = 127.0.0.1（little-endian）；任何 0.0.0.0／:: 綁定都算失敗
    assert bound and all(addr.startswith("0100007F:") for addr in bound), bound


def main() -> int:
    run([], READ + WRITE_DELETE)  # 新安裝（負責人 2026-10-25「預設全開」）
    run(WRITE_DELETE, READ)
    print("nextcloud child smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
