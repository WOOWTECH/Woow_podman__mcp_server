"""測試用：修補過的 odoo_mcp 副本＋真的 launcher（``woow-odoo-mcp``）＋假 Odoo（XML-RPC），經 Streamable HTTP 呼叫。

照 ``apps/odoo/Dockerfile`` 的順序在 tmp 的副本套上兩個建置期修補，以 PYTHONPATH 讓子行程載入這份副本——**不改 venv／
uv 快取裡的檔**（CI 的 venv 可能與 uv 快取共用 hardlink）。子行程擋掉 fastmcp（正式 odoo 映像沒有裝），工作目錄是空的
tmp 目錄（上游預設會讀工作目錄裡的 ``odoo_mcp_policy.json``）。假 Odoo 記下每個 ``execute_kw`` 的 model、method、
args、kwargs，測試以此判斷「真的送到 Odoo 了沒」。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

REPO = Path(__file__).resolve().parents[3]
PATCHES = REPO / "apps" / "odoo" / "patches"
#: 四個寫入開關（子行程只從這裡拿值，不繼承測試行程的環境）。
SWITCH_KEYS = ("ODOO_MCP_ENABLE_WRITES", "ODOO_MCP_ALLOW_DIRECT_WRITES", "ODOO_MCP_ALLOW_UNKNOWN_METHODS",
               "MCP_CHATTER_DIRECT")
#: 會改變上游行為、不能從測試行程繼承的其他環境變數。
_SCRUBBED = SWITCH_KEYS + ("ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", "ODOO_MCP_POLICY_FILE", "ODOO_MCP_ELICIT_WRITES",
                           "ODOO_MCP_RATE_LIMIT_MODE")
#: 假 Odoo 對讀取類方法的回應（不算寫入）。
READ_METHODS = frozenset({"fields_get", "search_read", "search", "search_count", "read", "context_get"})

_NO_FASTMCP = textwrap.dedent("""
    import sys

    class _NoFastMCP:
        def find_spec(self, name, path=None, target=None):
            if name == "fastmcp" or name.startswith("fastmcp."):
                raise ImportError("fastmcp is not installed in the odoo image: " + name)
            return None

    sys.meta_path.insert(0, _NoFastMCP())
""")


def load_patch(name: str):
    spec = importlib.util.spec_from_file_location(f"_woow_odoo_patch_{name}", PATCHES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_patched_site(dest: Path) -> Path:
    """在 *dest* 放一份已安裝 odoo_mcp 的副本並套上兩個建置期修補（與 Dockerfile 同順序），回傳 *dest*。"""
    source = Path(importlib.util.find_spec("odoo_mcp").origin).parent
    target = dest / "odoo_mcp"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    load_patch("fix_cross_session_token_store").patch(str(target / "server_core.py"))
    load_patch("fix_direct_writes").patch(str(target / "tools_write.py"))
    return dest


class FakeOdoo(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.calls: list[dict[str, Any]] = []

    def writes(self) -> list[tuple[str, str]]:
        """送到 Odoo、不是讀取的呼叫（model, method）。"""
        return [(c["model"], c["method"]) for c in self.calls if c["method"] not in READ_METHODS]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        server: FakeOdoo = self.server  # type: ignore[assignment]
        params, method = xmlrpc.client.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if method == "version":
            value: Any = {"server_version": "18.0", "server_version_info": [18, 0, 0, "final", 0]}
        elif method == "authenticate":
            value = 2
        else:
            model, odoo_method = (params[3], params[4]) if len(params) > 4 else ("", "")
            args = list(params[5]) if len(params) > 5 else []
            kwargs = dict(params[6]) if len(params) > 6 else {}
            kwargs.pop("context", None)  # 語言 context 由 client 自己加，與測試無關
            server.calls.append({"model": model, "method": odoo_method, "args": args, "kwargs": kwargs})
            value = {
                "create": 42,
                "copy": 43,
                "message_post": 7,
                "fields_get": {"name": {"type": "char", "string": "Name", "required": False, "readonly": False}},
                "search_read": [],
            }.get(odoo_method, True)
        data = xmlrpc.client.dumps((value,), methodresponse=True, allow_none=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _result(response) -> dict:
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    body = json.loads(data[-1]) if data else response.json()
    return body["result"]


def payload(result: dict) -> dict:
    """tools/call 結果的 JSON 內容（文字內容與 structuredContent 是同一份）。"""
    return json.loads(result["content"][0]["text"])


Call = Callable[[str, dict], Awaitable[dict]]


def run_child(site: Path, env: dict[str, str], tmp_path: Path,
              scenario: Callable[[Call, Callable[[str, dict], Awaitable[dict]]], Awaitable[Any]]) -> dict[str, Any]:
    """起真的 launcher（載入 *site* 的修補副本），執行 ``scenario(call_tool, rpc)``，回傳結果、假 Odoo 紀錄與 stderr。

    *env* 是子行程的開關與其他設定（四個開關沒給的就不設，與上游「沒設＝關」相同）。
    """
    odoo = FakeOdoo()
    threading.Thread(target=odoo.serve_forever, daemon=True).start()
    port = _free_port()
    code = _NO_FASTMCP + textwrap.dedent(f"""
        import odoo_mcp
        assert odoo_mcp.__file__.startswith({str(site)!r}), odoo_mcp.__file__
        sys.argv = ["woow-odoo-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "{port}"]
        from odoo_mcp_admin.launcher import main
        main()
    """)
    child_env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    workdir = tmp_path / "child-cwd"
    workdir.mkdir(exist_ok=True)
    child_env.update({
        "ODOO_URL": f"http://127.0.0.1:{odoo.server_address[1]}", "ODOO_DB": "d", "ODOO_USERNAME": "u",
        "ODOO_PASSWORD": "p", "ODOO_MCP_RETRY_ATTEMPTS": "0", "ODOO_MCP_LOG_FILE": str(tmp_path / "mcp.log"),
        "ODOO_MCP_RATE_LIMIT_MODE": "off",
        "PYTHONPATH": os.pathsep.join([str(site), child_env.get("PYTHONPATH", "")]).rstrip(os.pathsep),
        **env,
    })
    proc = subprocess.Popen([sys.executable, "-c", code], env=child_env, cwd=workdir, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}/mcp"

    async def main():
        async with httpx.AsyncClient(timeout=60) as client:
            headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            for _ in range(300):
                try:
                    response = await client.post(url, headers=headers, json={
                        "jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}}})
                    break
                except Exception:  # noqa: BLE001 — 子行程還在開機
                    if proc.poll() is not None:
                        raise RuntimeError("woow-odoo-mcp exited: " + (proc.stderr.read() or "")[-2000:])
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("woow-odoo-mcp did not start")
            assert response.status_code == 200, response.text
            headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
            headers["MCP-Protocol-Version"] = "2025-11-25"
            await client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            counter = iter(range(1, 10_000))

            async def rpc(method: str, params: dict) -> dict:
                response = await client.post(url, headers=headers, json={
                    "jsonrpc": "2.0", "id": next(counter), "method": method, "params": params})
                return _result(response)

            async def call_tool(name: str, arguments: dict) -> dict:
                return await rpc("tools/call", {"name": name, "arguments": arguments})

            return await scenario(call_tool, rpc)

    try:
        result = asyncio.run(asyncio.wait_for(main(), timeout=180))
    finally:
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate(timeout=5)
        odoo.shutdown()
    return {"result": result, "odoo": odoo, "stderr": err}
