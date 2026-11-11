"""hermes_config get／hermes_inspect 不回傳後端持有的憑證。

HA 測試機 2026-10-16 實測：在 Hermes dashboard 設好自訂模型供應商後，``hermes_config {action: "get"}``
（或 ``key: "custom_providers"``）把 ``custom_providers[].api_key`` 原文回給 MCP client。讀設定時改成保留
每個欄位名稱、看得出秘密有沒有設，但不回傳值；寫入（action=set）不變。以本機假 dashboard 跑真的工具函式。
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_mcp_server import server as hermes

PROVIDER_KEY = "sk-or-v1-backend-held-0123456789"
WEBHOOK_SECRET = "whsec-backend-held-4711"
CONFIG = {
    "model": {"default": "woow-free", "provider": "custom", "base_url": "https://proxy.test/v1"},
    "max_tokens": 4096, "auth_mode": "basic", "toolsets": ["web"], "max_live_sessions": 4,
    "custom_providers": [{"name": "litellm", "base_url": "https://user:url-pass-1234@proxy.test/v1",
                          "api_key": PROVIDER_KEY, "models": ["woow-free"]},
                         {"name": "unset", "api_key": "", "token": None}],
    "mcp_servers": {"github": {"url": "https://mcp.test",
                               "env": {"GITHUB_TOKEN": "ghp-backend-held-0815", "LOG_LEVEL": "info"}}},
    "secrets": {"OPENROUTER_API_KEY": "or-backend-held-2222"},
    "webhooks": {"signing": {"secret": WEBHOOK_SECRET, "enabled": True}},
}
def _merge(base: dict, incoming: dict) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


HELD = (PROVIDER_KEY, WEBHOOK_SECRET, "url-pass-1234", "ghp-backend-held-0815", "or-backend-held-2222")


@pytest.fixture
def dashboard(monkeypatch):
    writes: list[dict] = []

    class Dashboard(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Set-Cookie", "hermes_session_at=session-cookie-0815")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):
            assert self.path == "/api/config", self.path
            config = copy.deepcopy(CONFIG)
            for write in writes:  # Hermes 把 PUT 深合併進設定；寫入後讀回驗證要看得到
                _merge(config, write["config"])
            self._reply(config)

        def do_PUT(self):
            writes.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self._reply({"ok": True})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Dashboard)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("HERMES_DASHBOARD_URL", f"http://127.0.0.1:{httpd.server_port}")
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "dashboard-pass-0815")
    hermes._cookie_cache.clear()
    try:
        yield writes
    finally:
        httpd.shutdown()
        hermes._cookie_cache.clear()


def run(coro) -> dict:
    text = asyncio.run(coro)
    assert not any(secret in text for secret in HELD), text
    return json.loads(text)


def test_config_reads_never_return_backend_held_credentials(dashboard) -> None:
    everything = run(hermes.hermes_config(action="get"))
    assert everything["custom_providers"][0] == {"name": "litellm", "base_url": "https://user:[REDACTED]@proxy.test/v1",
                                                 "api_key": "[REDACTED]", "models": ["woow-free"]}
    assert everything["custom_providers"][1] == {"name": "unset", "api_key": "", "token": None}  # 「沒設定」仍看得到
    assert everything["mcp_servers"]["github"]["env"] == {"GITHUB_TOKEN": "[REDACTED]", "LOG_LEVEL": "info"}
    assert everything["secrets"] == {"OPENROUTER_API_KEY": "[REDACTED]"}
    assert everything["webhooks"]["signing"] == {"secret": "[REDACTED]", "enabled": True}
    assert everything["max_tokens"] == 4096 and everything["auth_mode"] == "basic"  # 不是秘密
    assert everything["model"] == CONFIG["model"]

    providers = run(hermes.hermes_config(action="get", key="custom_providers"))
    assert providers["value"][0]["api_key"] == "[REDACTED]" and providers["value"][0]["name"] == "litellm"
    assert run(hermes.hermes_config(action="get", key="secrets.OPENROUTER_API_KEY"))["value"] == "[REDACTED]"
    assert run(hermes.hermes_config(action="get", key="webhooks.signing"))["value"] == {"secret": "[REDACTED]",
                                                                                       "enabled": True}
    assert run(hermes.hermes_config(action="get", key="max_tokens"))["value"] == 4096

    summary = run(hermes.hermes_inspect(target="config"))
    assert summary["config_summary"] == {"model": CONFIG["model"], "toolsets": ["web"], "max_live_sessions": 4}


def test_config_writes_are_unchanged(dashboard) -> None:
    # 遮罩只作用在讀取；寫入原樣回傳值。（model.* 與任何憑證鍵自 AI-HMCP-001-3 起不能經 MCP 寫入，
    # 見 packages/mcp-admin-core/tests/test_hermes_security_guards.py，所以這裡改用一般鍵。）
    written = json.loads(asyncio.run(hermes.hermes_config(action="set", key="display.skin", value="new-key-9999")))
    assert written == {"ok": True, "key": "display.skin", "value": "new-key-9999"}
    # AI-HMCP-001-4：點號 key 寫成巢狀（以前送 {"display.skin": …} 會在 config 留下頂層字面鍵）
    assert dashboard == [{"config": {"display": {"skin": "new-key-9999"}}}]
    denied = json.loads(asyncio.run(hermes.hermes_config(action="set", key="model.api_key", value="new-key-9999")))
    assert "error" in denied and len(dashboard) == 1


@pytest.mark.parametrize("name,secret", [
    ("api_key", True), ("OPENAI_API_KEY", True), ("access_token", True), ("basic_auth", True), ("apiKey", True),
    ("password", True), ("client_secret", True), ("cookie", True),
    ("max_tokens", False), ("auth_mode", False), ("token_limit", False), ("keyboard", False), ("model", False),
])
def test_secret_key_names(name, secret) -> None:
    assert hermes._secret_key(name) is secret
