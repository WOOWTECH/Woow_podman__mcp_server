"""以本機暫存設定啟動真正的 n8n admin＋HTTP child；只讀套件 catalog，不連業務後端。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx


def free_port() -> int:
    """挑選測試用 loopback 埠。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def native_sqlite_loaded(admin_pid: int) -> bool:
    """admin 起的 n8n child（launcher exec 成 node）是否已把原生 better_sqlite3.node 載入行程。

    better-sqlite3 是 n8n-mcp 的 optional dependency，裝不起來時上游靜默退回 sql.js；在真的 child 上確認。
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", encoding="utf-8") as status:
                ppid = next(int(line.split()[1]) for line in status if line.startswith("PPid:"))
            with open(f"/proc/{entry}/cmdline", "rb") as cmdline:
                argv = cmdline.read().split(b"\0")
            if ppid != admin_pid or not any(arg.endswith(b"dist/mcp/index.js") for arg in argv):
                continue
            with open(f"/proc/{entry}/maps", encoding="utf-8") as maps:
                return "better_sqlite3.node" in maps.read()
        except (OSError, StopIteration, ValueError):
            continue
    raise AssertionError("n8n child process not found")


def response_rpc(response: httpx.Response) -> dict:
    """解讀 JSON 或單次 Streamable HTTP SSE 回應。"""
    assert response.status_code == 200, f"MCP HTTP status {response.status_code}"
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise AssertionError("MCP response has no data event")
    return response.json()


def main() -> None:
    """使用暫存 DB 與隨機假 token 執行只讀協定驗證。"""
    admin_port, child_port = free_port(), free_port()
    token, child_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="n8n-security-smoke-") as directory:
        config = Path(directory) / "config.json"
        config.write_text(json.dumps({
            "mcp_auth_token": token,
            "mcp_server": {"port": child_port, "env": {"AUTH_TOKEN": child_token}},
            "proxy": {"bearer_token": child_token},
        }))
        env = {k: v for k, v in os.environ.items() if not k.startswith("N8N_API")}
        env.pop("N8N_MCP_ACCESS_TOKEN", None)
        env["MCP_ADMIN_CONFIG"] = str(config)
        with tempfile.TemporaryFile(mode="w+b") as log:
            proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "n8n_mcp_admin.main:app",
                                     "--host", "127.0.0.1", "--port", str(admin_port)],
                                    env=env, cwd=directory, stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{admin_port}", timeout=15) as client:
                    headers = {"accept": "application/json, text/event-stream"}
                    endpoint = f"/private_{token}/mcp"
                    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "ci-security-smoke", "version": "1"}}}
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        try:
                            response = client.post(endpoint, json=initialize, headers=headers)
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        assert proc.poll() is None, "admin process exited"
                        time.sleep(0.25)
                    else:
                        raise AssertionError("n8n HTTP child did not initialize")
                    assert "result" in response_rpc(response)
                    headers["mcp-session-id"] = response.headers["mcp-session-id"]
                    client.post(endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
                    listed = response_rpc(client.post(endpoint, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers))
                    names = {tool["name"] for tool in listed["result"]["tools"]}
                    assert {"search_nodes", "validate_workflow"} <= names
                    workflow_schema = next(tool["outputSchema"] for tool in listed["result"]["tools"]
                                           if tool["name"] == "validate_workflow")
                    for issue_kind in ("errors", "warnings"):
                        assert workflow_schema["properties"][issue_kind]["items"]["properties"]["details"] == {"type": "object"}
                    base_workflow = {
                        "name": "schema-smoke",
                        "nodes": [
                            {"id": "start", "name": "Start", "type": "n8n-nodes-base.manualTrigger",
                             "typeVersion": 1, "position": [0, 0], "parameters": {}},
                            {"id": "end", "name": "End", "type": "n8n-nodes-base.noOp",
                             "typeVersion": 1, "position": [200, 0], "parameters": {}},
                        ],
                        "connections": {"Start": {"main": [[{"node": "End", "type": "main", "index": 0}]]}},
                    }
                    warning_workflow = {**base_workflow, "nodeGroups": [
                        {"id": "g1", "name": "G", "nodeIds": ["start"]}]}
                    error_workflow = {**base_workflow, "nodes": [base_workflow["nodes"][0],
                        {**base_workflow["nodes"][1], "parameters": {"onError": "continueErrorOutput"}}]}
                    for call_id, workflow, valid, issue_kind in (
                        (10, warning_workflow, True, "warnings"),
                        (11, error_workflow, False, "errors"),
                    ):
                        call = {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                                "params": {"name": "validate_workflow", "arguments": {"workflow": workflow}}}
                        result = response_rpc(client.post(endpoint, json=call, headers=headers))["result"]
                        assert result.get("isError") is not True
                        structured = result["structuredContent"]
                        assert structured["valid"] is valid
                        assert any(isinstance(issue.get("details"), dict)
                                   for issue in structured[issue_kind])
                    normal = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "search_nodes", "arguments": {"query": "webhook", "limit": 1}}}
                    result = response_rpc(client.post(endpoint, json=normal, headers=headers))
                    assert "result" in result and not result["result"].get("isError"), "local catalog lookup failed"
                    # catalog 查詢成功代表 DB 已開啟：此時 child 必須用原生 better-sqlite3（不是 sql.js 後備）。
                    assert native_sqlite_loaded(proc.pid), "n8n child fell back to sql.js (better-sqlite3 not loaded)"
                    oversized = {**normal, "params": {"name": "validate_workflow", "arguments": {"items": [0] * 10_000}}}
                    for path in ("mcp", "messages/?session_id=test", "sse"):
                        denied = client.post(f"/private_{token}/{path}", json=oversized, headers=headers)
                        assert denied.status_code == 413 and denied.json()["error"]["code"] == -32602
                    assert client.post(endpoint, content=b" " * (4 * 1024 * 1024 + 1), headers=headers).status_code == 413
                    # 超量請求後，相同 child/session 仍可列 catalog。
                    assert "result" in response_rpc(client.post(endpoint, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, headers=headers))
                    with client.stream("GET", f"/private_{token}/sse", headers={"accept": "text/event-stream"}) as stream:
                        assert stream.status_code == 200
                        for line in stream.iter_lines():
                            if line.startswith("data: "):
                                assert f"/private_{token}/" in line and "messages" in line
                                break
                        else:
                            raise AssertionError("Legacy SSE stream has no endpoint event")
                    print(json.dumps({"n8n_real_child": True, "backend_free_tools": len(names),
                                      "catalog_call": True, "native_sqlite": True, "streamable_http": True, "legacy_sse": True,
                                      "input_limits": True, "healthy_after_denial": True}))
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
