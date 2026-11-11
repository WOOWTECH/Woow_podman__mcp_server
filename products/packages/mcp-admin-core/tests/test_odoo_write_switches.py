"""Odoo 四個寫入開關在真的子行程裡的效果（負責人 2026-10-25「預設全開」）。

在 tmp 複製一份 ``odoo_mcp`` 套件、照 Dockerfile 的順序套上 ``apps/odoo/patches/`` 的兩個建置期修補，再用
PYTHONPATH 讓真的 launcher（``woow-odoo-mcp``）載入這份副本——**不改 venv／uv 快取裡的檔**（CI 的 venv 可能與
uv 快取共用 hardlink，2026-10-24 就曾因此改壞快取）。子行程擋掉 fastmcp（正式 odoo 映像沒有裝），接一個假 Odoo
（XML-RPC），每種設定各起一次：

- 全開（``seed_defaults.FRESH_ENV``，四個都開）：tools/list 41 支（三支審批工具照樣列出，同 HA 0.4.0）、
  ``execute_method`` 的 ``method`` 參數與 ``chatter_post`` 的說明是直接模式；直接 create、呼叫未審查的
  action_confirm 都送到 Odoo；``chatter_post`` 第一次呼叫就張貼。
- 只開直接寫入（未審查方法關）：說明改成「create／write／unlink 直接執行、未審查的方法仍擋」。
- 既有服務缺鍵（``seed_defaults.LEGACY_DEFAULTS``，只開審核寫入）：41 支；說明寫明直接寫入關、create／write／unlink
  走三階段審批、chatter 先預覽（第三輪起每種組合都有如實的說明，見 ``odoo_mcp_admin.write_gates``）；直接 create 與
  未審查方法被擋、``chatter_post`` 先回預覽。

16 種組合的完整矩陣（含總閘門）在 ``test_odoo_write_gates.py``。
"""

from __future__ import annotations

import asyncio
import hashlib
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

import pytest

pytest.importorskip("odoo_mcp")
pytest.importorskip("odoo_mcp_admin")
httpx = pytest.importorskip("httpx")

from odoo_mcp_admin.seed_defaults import FRESH_ENV, LEGACY_DEFAULTS  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
PATCHES = REPO / "apps" / "odoo" / "patches"
APPROVAL_TOOLS = {"preview_write", "validate_write", "execute_approved_write"}
#: MCP_CHATTER_DIRECT 關時 chatter_post 的說明（第三輪起寫明預覽→approval＋confirm=true 的流程）。
APPROVAL_CHATTER_TEXT = (
    "Post a chatter message on a mail.thread record. Approval mode is on in this deployment (MCP_CHATTER_DIRECT "
    "is off): the first call only returns a preview with an approval; call chatter_post again with the same "
    "arguments plus that approval and confirm=true to post the message."
)

#: 子行程開頭：拒絕 fastmcp（正式 odoo 映像沒有裝它），之後才 import 任何東西。
_NO_FASTMCP = textwrap.dedent("""
    import sys

    class _NoFastMCP:
        def find_spec(self, name, path=None, target=None):
            if name == "fastmcp" or name.startswith("fastmcp."):
                raise ImportError("fastmcp is not installed in the odoo image: " + name)
            return None

    sys.meta_path.insert(0, _NoFastMCP())
""")


def _load_patch(name: str):
    spec = importlib.util.spec_from_file_location(f"_woow_odoo_patch_{name}", PATCHES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def patched_site(tmp_path_factory) -> Path:
    """已安裝 odoo_mcp 的副本＋兩個建置期修補（與 apps/odoo/Dockerfile 同順序）。"""
    source = Path(importlib.util.find_spec("odoo_mcp").origin).parent
    site = tmp_path_factory.mktemp("odoo-site")
    target = site / "odoo_mcp"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    names = ("tools_write.py", "write_policy.py", "server_core.py")
    before = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}
    _load_patch("fix_cross_session_token_store").patch(str(target / "server_core.py"))
    direct_writes = _load_patch("fix_direct_writes")
    assert hashlib.sha256((target / "server_core.py").read_bytes()).hexdigest() == direct_writes.SERVER_CORE_PINNED
    assert direct_writes.patch(str(target / "tools_write.py")) is True
    assert direct_writes.patch(str(target / "tools_write.py")) is False  # 冪等
    assert not (PATCHES / "fix_remove_3step_tools.py").exists(), "三支審批工具不再隱藏（負責人 2026-10-25）"
    assert hashlib.sha256((target / "tools_write.py").read_bytes()).hexdigest() == direct_writes.PATCHED
    assert hashlib.sha256((target / "write_policy.py").read_bytes()).hexdigest() == direct_writes.WRITE_POLICY_PATCHED
    assert hashlib.sha256((target / "server_core.py").read_bytes()).hexdigest() == direct_writes.SERVER_CORE_PATCHED
    # 守線：沒有改到 venv 裡的原檔
    assert {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names} == before
    assert before["tools_write.py"] == direct_writes.PINNED and before["write_policy.py"] == direct_writes.WRITE_POLICY_PINNED
    return site


def test_direct_writes_patch_refuses_unknown_content(tmp_path):
    module = _load_patch("fix_direct_writes")
    target = tmp_path / "tools_write.py"
    target.write_text("def execute_method():\n    pass\n")
    with pytest.raises(ValueError, match="unexpected tools_write.py"):
        module.patch(str(target))


def test_direct_writes_patch_is_all_or_nothing(tmp_path):
    """server_core.py 不是 token store 修補後的內容（例如順序錯了）：三個檔一個都不寫。"""
    module = _load_patch("fix_direct_writes")
    source = Path(importlib.util.find_spec("odoo_mcp").origin).parent
    package = tmp_path / "odoo_mcp"
    shutil.copytree(source, package, ignore=shutil.ignore_patterns("__pycache__"))
    before = {name: (package / name).read_bytes() for name in ("tools_write.py", "write_policy.py", "server_core.py")}
    with pytest.raises(ValueError, match="unexpected server_core.py"):
        module.patch_package(str(package))
    assert {name: (package / name).read_bytes() for name in before} == before


# ---- 假 Odoo（XML-RPC）---------------------------------------------------------------------------


class _FakeOdoo(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.calls: list[tuple[str, str]] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        server: _FakeOdoo = self.server  # type: ignore[assignment]
        params, method = xmlrpc.client.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if method == "version":
            value = {"server_version": "18.0", "server_version_info": [18, 0, 0, "final", 0]}
        elif method == "authenticate":
            value = 2
        else:
            model, odoo_method = (params[3], params[4]) if len(params) > 4 else ("", "")
            server.calls.append((model, odoo_method))
            value = {"create": 42, "message_post": 7}.get(odoo_method, True)
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


async def _session(client, url: str) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    for _ in range(300):
        try:
            response = await client.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
            break
        except Exception:  # noqa: BLE001 — 子行程還在開機
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("woow-odoo-mcp did not start")
    assert response.status_code == 200, response.text
    headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
    headers["MCP-Protocol-Version"] = "2025-11-25"
    await client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return headers


def _result(response) -> dict:
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    body = json.loads(data[-1]) if data else response.json()
    return body["result"]


def _run_child(site: Path, switches: dict[str, str], tmp_path: Path) -> dict:
    """起真的 launcher（載入修補過的副本），回傳 tools/list 與三個 tools/call 的結果。"""
    odoo = _FakeOdoo()
    threading.Thread(target=odoo.serve_forever, daemon=True).start()
    port = _free_port()
    code = _NO_FASTMCP + textwrap.dedent(f"""
        import odoo_mcp
        assert odoo_mcp.__file__.startswith({str(site)!r}), odoo_mcp.__file__
        sys.argv = ["woow-odoo-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "{port}"]
        from odoo_mcp_admin.launcher import main
        main()
    """)
    env = {k: v for k, v in os.environ.items() if k not in FRESH_ENV}
    env.update({
        "ODOO_URL": f"http://127.0.0.1:{odoo.server_address[1]}", "ODOO_DB": "d", "ODOO_USERNAME": "u",
        "ODOO_PASSWORD": "p", "ODOO_MCP_RETRY_ATTEMPTS": "0", "ODOO_MCP_LOG_FILE": str(tmp_path / "mcp.log"),
        "PYTHONPATH": os.pathsep.join([str(site), env.get("PYTHONPATH", "")]).rstrip(os.pathsep),
        **switches,
    })
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}/mcp"

    async def scenario():
        async with httpx.AsyncClient(timeout=60) as client:
            headers = await _session(client, url)

            async def call(rpc_id, method, params):
                response = await client.post(url, headers=headers, json={
                    "jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
                return _result(response)

            tools = (await call(1, "tools/list", {}))["tools"]
            create = await call(2, "tools/call", {"name": "execute_method", "arguments": {
                "model": "res.partner", "method": "create", "args": [{"name": "MCP 全開測試"}]}})
            confirm = await call(3, "tools/call", {"name": "execute_method", "arguments": {
                "model": "sale.order", "method": "action_confirm", "args": [[1]]}})
            chatter = await call(4, "tools/call", {"name": "chatter_post", "arguments": {
                "model": "res.partner", "record_id": 1, "body": "MCP 全開測試"}})
            health = await call(5, "tools/call", {"name": "health_check", "arguments": {}})
            return tools, create, confirm, chatter, health

    try:
        tools, create, confirm, chatter, health = asyncio.run(asyncio.wait_for(scenario(), timeout=180))
    finally:
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate(timeout=5)
        odoo.shutdown()
    by_name = {t["name"]: t for t in tools}
    return {
        "names": set(by_name),
        "method_text": by_name["execute_method"]["inputSchema"]["properties"]["method"].get("description"),
        "chatter_text": by_name["chatter_post"].get("description"),
        "create": create, "confirm": confirm, "chatter": chatter,
        "runtime": json.loads(health["content"][0]["text"])["runtime"],
        "odoo_calls": list(odoo.calls), "stderr": err,
    }


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_fresh_install_switches_open_everything(patched_site, tmp_path):
    out = _run_child(patched_site, dict(FRESH_ENV), tmp_path)

    assert len(out["names"]) == 41 and APPROVAL_TOOLS <= out["names"], sorted(out["names"])
    assert out["method_text"].startswith("Odoo model method to call; any method runs directly in this deployment")
    assert "execute_approved_write flow is optional" in out["method_text"]
    assert "ODOO_MCP_ALLOW_UNKNOWN_METHODS is set" in out["method_text"]
    assert out["chatter_text"].startswith("Post a chatter message on a mail.thread record. Direct mode is on")

    assert out["create"].get("isError") is not True, out["create"]
    assert _payload(out["create"])["result"] == 42
    assert out["runtime"]["direct_writes_enabled"] is True  # health_check 看得出直接寫入開著
    assert out["runtime"]["write_execution_enabled"] is True
    assert out["confirm"].get("isError") is not True, out["confirm"]
    assert _payload(out["chatter"])["mode"] == "direct"
    assert ("res.partner", "create") in out["odoo_calls"]
    assert ("sale.order", "action_confirm") in out["odoo_calls"]
    assert ("res.partner", "message_post") in out["odoo_calls"]


def test_direct_writes_without_unknown_methods_text(patched_site, tmp_path):
    switches = {**FRESH_ENV, "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "0", "MCP_CHATTER_DIRECT": "0"}
    out = _run_child(patched_site, switches, tmp_path)

    assert out["method_text"].startswith("Odoo model method to call; create, write and unlink run directly")
    assert "unreviewed side-effect methods stay blocked" in out["method_text"]
    assert "calls that post chatter messages are refused because MCP_CHATTER_DIRECT is off" in out["method_text"]
    assert out["chatter_text"] == APPROVAL_CHATTER_TEXT
    assert out["create"].get("isError") is not True, out["create"]
    assert out["runtime"]["direct_writes_enabled"] is True
    assert out["confirm"].get("isError") is True
    assert "Unreviewed side-effect methods are blocked" in out["confirm"]["content"][0]["text"]
    assert _payload(out["chatter"])["mode"] == "preview"


def test_existing_service_defaults_keep_the_approval_flow(patched_site, tmp_path):
    out = _run_child(patched_site, dict(LEGACY_DEFAULTS), tmp_path)

    assert len(out["names"]) == 41 and APPROVAL_TOOLS <= out["names"]
    assert out["method_text"].startswith(
        "Odoo model method to call; direct writes are off in this deployment (ODOO_MCP_ALLOW_DIRECT_WRITES is off)")
    assert "preview_write -> validate_write -> execute_approved_write" in out["method_text"]
    assert out["chatter_text"] == APPROVAL_CHATTER_TEXT
    assert out["create"].get("isError") is True
    assert "Direct execute_method blocks create/write/unlink" in out["create"]["content"][0]["text"]
    assert out["runtime"]["direct_writes_enabled"] is False  # 擋下 create 的同一個判斷
    assert out["runtime"]["write_execution_enabled"] is True  # 審核寫入（ENABLE_WRITES）是另一個開關
    assert out["confirm"].get("isError") is True
    assert _payload(out["chatter"])["mode"] == "preview"
    assert not any(call[1] in ("create", "action_confirm", "message_post") for call in out["odoo_calls"])


def test_health_check_and_the_guard_share_one_predicate(patched_site, tmp_path):
    """"on" 是 truthy_env 認的值、guard 不認：health_check 必須跟著 guard（不能自己另用 truthy_env）。"""
    out = _run_child(patched_site, {**FRESH_ENV, "ODOO_MCP_ALLOW_DIRECT_WRITES": "on"}, tmp_path)
    assert out["create"].get("isError") is True
    assert "Direct execute_method blocks create/write/unlink" in out["create"]["content"][0]["text"]
    assert out["runtime"]["direct_writes_enabled"] is False
    assert "ODOO_MCP_ALLOW_DIRECT_WRITES is off" in out["method_text"]  # 說明也是同一個判斷
    assert ("res.partner", "create") not in out["odoo_calls"]
