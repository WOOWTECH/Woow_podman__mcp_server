"""每個 Python MCP 子行程都收得下 16 MiB 的請求（mcp 1.30.0 預設 4 MiB，超過就在處理前回 413）。

以各 app 的 LaunchPolicy（正式環境的 command／args）與 core 組 env 的同一段程式啟動真的子行程（不連任何後端），
再以 ping（params._meta 補足長度，任何 MCP server 都接受）量 Streamable HTTP 的上限：約 5 MiB 與剛好 16 MiB 收、
多 1 byte 回 413，之後同一個 session 照常可用。hermes、opendesign 走 core DualTransportApp，另驗 legacy SSE。
經閘道的上傳端到端見 apps/nextcloud/tests/test_child_process.py。
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from mcp_admin_core.process import _build_subprocess_env
from mcp_admin_core.request_body import MAX_REQUEST_BODY_BYTES

MiB = 1024 * 1024
DEAD = "http://127.0.0.1:9"  # 不會有人聽的埠：ping 與握手都不需要後端
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "mcp-protocol-version": "2025-06-18"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}

# product → (admin 模組（LAUNCH_POLICY）、connection、mcp_server.env)
PRODUCTS = {
    "hermes": ("hermes_mcp_admin.main",
               {"gateway_url": DEAD, "gateway_api_key": "k", "dashboard_url": DEAD,
                "dashboard_username": "u", "dashboard_password": "p"}, {}),
    "opendesign": ("opendesign_mcp_admin.main", {"od_api_base": DEAD}, {}),
    "odoo": ("odoo_mcp_admin.main",
             {"odoo_url": DEAD, "odoo_db": "d", "odoo_username": "u", "odoo_password": "p"}, {}),
    "litellm": ("litellm_mcp_admin.main", {"base_url": DEAD, "master_key": "sk-test"},
                {"LITELLM_MCP_READONLY": "1"}),
    "nextcloud": ("nextcloud_mcp_admin.main", {"base_url": DEAD + "/nc", "username": "u", "app_password": "p"}, {}),
}
LEGACY_SSE = {"hermes", "opendesign"}  # core DualTransportApp 另外提供 /sse＋/messages/


def ping(size: int, rpc_id: int = 2) -> bytes:
    shell = {"jsonrpc": "2.0", "id": rpc_id, "method": "ping", "params": {"_meta": {"padding": ""}}}
    empty = len(json.dumps(shell, separators=(",", ":")))
    shell["params"]["_meta"]["padding"] = "A" * (size - empty)
    body = json.dumps(shell, separators=(",", ":")).encode()
    assert len(body) == size
    return body


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def spawn(product: str, tmp_path: Path):
    """以正式環境的 LaunchPolicy 與 env 組法啟動子行程；回傳 base URL。"""
    module, connection, env = PRODUCTS[product]
    policy = importlib.import_module(module).LAUNCH_POLICY
    port = _free_port()
    command = policy.build_command(port)
    executable = shutil.which(command[0], path=os.pathsep.join([str(Path(sys.executable).parent),
                                                                 os.environ.get("PATH", "")]))
    assert executable, f"console script {command[0]} not installed (uv sync --all-packages)"
    child_env = _build_subprocess_env(connection, {"env": env}, policy, {"mcp_server": {"env": env}})
    child_env["MCP_ADMIN_CONFIG"] = str(tmp_path / "absent-config.json")  # nextcloud：讀不到政策就唯讀啟動
    log = tmp_path / f"{product}.log"
    with log.open("wb") as out:
        proc = subprocess.Popen([executable, *command[1:]], env=child_env, cwd=tmp_path,
                                stdout=out, stderr=subprocess.STDOUT)
    try:
        yield f"http://127.0.0.1:{port}", proc, log
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


async def open_session(client: httpx.AsyncClient, url: str, proc, log: Path) -> dict:
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        try:
            response = await client.post(url, json=INIT, headers=HEADERS)
            break
        except httpx.TransportError:
            if proc.poll() is not None or asyncio.get_running_loop().time() > deadline:
                raise AssertionError("MCP child did not start:\n" + log.read_text(errors="replace")[-3000:])
            await asyncio.sleep(0.25)
    assert response.status_code == 200, response.text[:500]
    headers = dict(HEADERS)
    if response.headers.get("mcp-session-id"):  # opendesign 是 stateless：沒有 session id
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
    await client.post(url, json=INITIALIZED, headers=headers)
    return headers


def _reply(response: httpx.Response, rpc_id: int) -> dict:
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            message = json.loads(line[5:])
            if message.get("id") == rpc_id:
                return message
    raise AssertionError(f"no reply for id {rpc_id}: {response.text[:300]}")


async def _next_data(lines, rpc_id=None) -> str:
    async def read():
        async for line in lines:
            if line.startswith("data:"):
                data = line[5:].strip()
                if rpc_id is None or json.loads(data).get("id") == rpc_id:
                    return data
        raise AssertionError("SSE stream ended")
    return await asyncio.wait_for(read(), 30)


async def check_legacy_sse(client: httpx.AsyncClient, base: str) -> None:
    async with client.stream("GET", base + "/sse", headers={"Accept": "text/event-stream"}) as stream:
        assert stream.status_code == 200
        lines = stream.aiter_lines()
        endpoint = base + await _next_data(lines)  # /messages/?session_id=…
        post_headers = {"Content-Type": "application/json"}
        assert (await client.post(endpoint, json=INIT, headers=post_headers)).status_code == 202
        await _next_data(lines, 1)
        assert (await client.post(endpoint, json=INITIALIZED, headers=post_headers)).status_code == 202
        for rpc_id, size in ((2, 5 * MiB), (3, MAX_REQUEST_BODY_BYTES)):
            accepted = await client.post(endpoint, content=ping(size, rpc_id), headers=post_headers)
            assert accepted.status_code == 202, (size, accepted.text[:200])
            assert json.loads(await _next_data(lines, rpc_id))["result"] == {}
        refused = await client.post(endpoint, content=ping(MAX_REQUEST_BODY_BYTES + 1, 4), headers=post_headers)
        assert refused.status_code == 413


@pytest.mark.parametrize("product", sorted(PRODUCTS))
async def test_python_child_accepts_bodies_up_to_the_ceiling(product, tmp_path):
    with spawn(product, tmp_path) as (base, proc, log):
        async with httpx.AsyncClient(timeout=60) as client:
            url = base + "/mcp"
            headers = await open_session(client, url, proc, log)
            for rpc_id, size in ((2, 5 * MiB), (3, MAX_REQUEST_BODY_BYTES)):
                response = await client.post(url, content=ping(size, rpc_id), headers=headers)
                assert response.status_code == 200, (size, response.text[:300])
                assert _reply(response, rpc_id)["result"] == {}
            # 超過上限：子行程自己在處理前就拒收（經閘道時由閘道先擋並回可讀的錯誤，見 test_request_body_limit.py）
            refused = await client.post(url, content=ping(MAX_REQUEST_BODY_BYTES + 1, 4), headers=headers)
            assert refused.status_code == 413
            after = await client.post(url, content=ping(1024, 5), headers=headers)
            assert after.status_code == 200 and _reply(after, 5)["result"] == {}
            if product in LEGACY_SSE:
                await check_legacy_sse(client, base)
