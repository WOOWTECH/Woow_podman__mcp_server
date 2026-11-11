"""CI smoke（在已建好的 k8s-mcp-admin 映像容器內執行）：launcher＋映像裡的上游 kubernetes-mcp-server 起得來、
只綁 127.0.0.1，tools/list 與 ``tool_registry.expected_exposed_tools()`` 一致：

- 全開（``launcher.FRESH_ENV``，負責人 2026-10-25「預設全開」）：六個免設定工具集 34 支、TOML 沒有 Secret 封鎖；
  多叢集預設開，kubeconfig 有兩個 context 時多出 ``configuration_contexts_list``（35 支）。
- 2026-10-25 之前的全鎖 seed（唯讀＋擋破壞性、擋 Secret、core,config,helm、單叢集）：15 支（兩個 context 也不會
  出現 configuration_contexts_list），TOML 擋 Secret。

不連任何叢集（kubeconfig 指向不存在的位址；列工具不需要叢集）。

用法（CI）：docker exec -i smoke-k8s python - < apps/k8s/scripts/ci_smoke_child.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request

from k8s_mcp_admin import tool_registry
from k8s_mcp_admin.launcher import FRESH_ENV

LOCKED_ENV = {
    "K8S_MCP_READ_ONLY": "1",
    "K8S_MCP_DISABLE_DESTRUCTIVE": "1",
    "K8S_MCP_ALLOW_SECRETS": "0",
    "K8S_MCP_TOOLSETS": "core,config,helm",
}

KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- name: smoke
  cluster:
    server: https://127.0.0.1:9
contexts:
- name: smoke
  context: {cluster: smoke, user: smoke}
current-context: smoke
users:
- name: smoke
  user: {token: ci-smoke-not-a-real-token}
"""

KUBECONFIG_TWO_CONTEXTS = """apiVersion: v1
kind: Config
clusters:
- name: smoke-a
  cluster:
    server: https://127.0.0.1:9
- name: smoke-b
  cluster:
    server: https://127.0.0.1:10
contexts:
- name: smoke-a
  context: {cluster: smoke-a, user: smoke}
- name: smoke-b
  context: {cluster: smoke-b, user: smoke}
current-context: smoke-a
users:
- name: smoke
  user: {token: ci-smoke-not-a-real-token}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(url: str, body: dict, session: str | None = None) -> tuple[dict | None, str | None]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode()
        session = response.headers.get("Mcp-Session-Id") or session
    data = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
    raw = data[-1] if data else raw
    return (json.loads(raw) if raw.strip() else None), session


def list_tools(port: int, timeout: float = 60.0) -> list[str]:
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            _, session = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ci-smoke", "version": "0"}}})
            post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
            result, _ = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session)
            return sorted(tool["name"] for tool in result["result"]["tools"])
        except Exception as exc:  # noqa: BLE001 — 子行程還在開機
            last = exc
            time.sleep(0.3)
    raise SystemExit(f"kubernetes-mcp-server did not answer on {url}: {last!r}")


def run(governance: dict[str, str], expected: set[str], secrets_allowed: bool, kubeconfig: str = KUBECONFIG) -> None:
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        config = os.path.join(tmp, "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"tools": {"disabled": []}}, fh)
        env = {**os.environ, "MCP_ADMIN_CONFIG": config, "K8S_MCP_KUBECONFIG": kubeconfig,
               "K8S_MCP_IN_CLUSTER": "false", **governance}
        proc = subprocess.Popen(["k8s-mcp-http", "--port", str(port)], env=env, cwd=tmp,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            names = list_tools(port)
            listening = subprocess.run(["sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"],
                                       capture_output=True, text=True).stdout
            with open(os.path.join(tmp, "k8s-mcp.toml"), "rb") as fh:
                toml = tomllib.load(fh)
        finally:
            proc.terminate()
            try:
                output, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate()
    contexts = kubeconfig.count("- name: smoke-") // 2 or 1
    print(f"governance={governance} contexts={contexts} -> {len(names)} tools: {names}")
    assert set(names) == expected, (sorted(set(names) - expected), sorted(expected - set(names)), output[-2000:])
    assert ("denied_resources" not in toml) is secrets_allowed, toml
    assert "ci-smoke-not-a-real-token" not in output, "kubeconfig token leaked into child output"
    port_hex = f"{port:04X}"
    bound = [line.split()[1] for line in listening.splitlines()[1:] if line.split()[1].endswith(f":{port_hex}")]
    # 0100007F = 127.0.0.1（little-endian）；任何 0.0.0.0／:: 綁定都算失敗
    assert bound and all(addr.startswith("0100007F:") for addr in bound), bound


def main() -> int:
    toolsets = FRESH_ENV["K8S_MCP_TOOLSETS"].split(",")
    fresh = tool_registry.expected_exposed_tools(read_only=False, disable_destructive=False, toolsets=toolsets)
    assert len(fresh) == 34, len(fresh)
    run(dict(FRESH_ENV), fresh, secrets_allowed=True)
    fresh_multi = tool_registry.expected_exposed_tools(read_only=False, disable_destructive=False, toolsets=toolsets,
                                                       multiple_contexts=True)
    assert fresh_multi - fresh == {"configuration_contexts_list"}, sorted(fresh_multi - fresh)
    run(dict(FRESH_ENV), fresh_multi, secrets_allowed=True, kubeconfig=KUBECONFIG_TWO_CONTEXTS)
    locked = tool_registry.expected_exposed_tools(read_only=True, disable_destructive=True,
                                                  toolsets=["core", "config", "helm"])
    assert len(locked) == 15, len(locked)
    run(LOCKED_ENV, locked, secrets_allowed=False, kubeconfig=KUBECONFIG_TWO_CONTEXTS)
    print("k8s child smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
