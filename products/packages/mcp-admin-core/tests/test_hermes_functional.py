"""Hermes MCP 工具的功能缺陷（商用驗收 AI-HMCP-001-2／-4／-5／-6／-7／-8、AI-HMCP-003-1）。

QA 服務 Hermes MCP 249 → Hermes 247（0.21.6）實測，見 ledger/defects/AI-HMCP-*.json：

- -001-2：``hermes_model set`` 只送 {model[,provider]}，0.21.6 的 ``POST /api/model/set`` 要求 scope＋provider，
  永遠 422；``list_providers`` 是寫死清單，與 ``/api/model/options`` 不一致。
- -001-4：``hermes_config set display.compact`` 把點號 key 當頂層鍵 PUT，config 多一個字面鍵、回 ok:true。
- -001-5：``hermes_chat`` 忽略 session_id，每輪都是新 session，沒有多輪上下文。
- -001-6：``hermes_chat`` 固定 30 秒逾時、訊息空白，Hermes 端那一輪還繼續跑。
- -001-7：``hermes_webhook`` 說明教 ``{{payload}}``，Hermes 只認 ``{欄位.路徑}``／``{__raw__}``。
- -001-8：刪除不存在的 session 回 ok:true；刪除不存在的 webhook 回 httpx 原文。
- -003-1：dashboard cookie 快取 10 分鐘、以 URL 為鍵，遇 401 不清除。

本檔保留 0.21.6 功能案例，session mutation 使用 published Agent 0.22.5 的 fences/receipts 合約。
假 Hermes（httpx.MockTransport）走真的 MCP 協定（in-memory client session →
FastMCP），檢查 client 實際拿到的 ``CallToolResult`` 與送到 Hermes 的請求本文。
"""
from __future__ import annotations

import asyncio
import copy
import json
import time

import httpx
import pytest
from mcp.client import Client

from hermes_mcp_server import server as hermes_server

GATEWAY = "http://hermes-agent-svc:8642"
DASHBOARD = "http://hermes-agent-svc:9119"
_RealAsyncClient = httpx.AsyncClient


def _deep_merge(base: dict, incoming: dict) -> dict:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class _EventStream(httpx.AsyncByteStream):
    """SSE 本文；``hang=True`` 時送完事件後只送 keepalive，不結束。記錄 client 有沒有把連線關掉。"""

    def __init__(self, events: list[tuple[str, dict]], *, hang: bool = False, owner=None):
        self.events = events
        self.hang = hang
        self.owner = owner

    async def __aiter__(self):
        for name, data in self.events:
            yield f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
        while self.hang:
            await asyncio.sleep(0.02)
            yield b": keepalive\n\n"

    async def aclose(self) -> None:
        if self.owner is not None:
            self.owner.stream_closed = True


class FakeHermes:
    """Synthetic functional backend; session writes require the published Agent's fenced contract."""

    def __init__(self):
        self.config = {"display": {"compact": False, "skin": "default"}, "model": {"default": "m0", "provider": "custom"}}
        self.model = {"model": "qwen/qwen3.8-flash", "provider": "custom"}
        self.sessions = {"20261016_193839_d56dfbc7": {
            "id": "20261016_193839_d56dfbc7", "runtime_revision": 7, "runtime_generation": 3}}
        self.session_receipts = {}
        self.webhooks = {"orders": {"prompt": "x", "enabled": True}}
        self.requests: list[httpx.Request] = []
        self.bodies: list[tuple[str, str, object]] = []
        self.ignore_config_writes = False
        self.model_set_response: dict | None = None
        self.chat_hang = False
        self.stream_closed = False
        self.valid_cookie = "tok"
        self.password = "dash-pw"
        self.logins = 0

    # -- helpers ---------------------------------------------------------
    def body(self, method: str, path: str):
        for m, p, b in self.bodies:
            if m == method and p == path:
                return b
        raise AssertionError(f"沒有送出 {method} {path}；實際：{[(m, p) for m, p, _ in self.bodies]}")

    def sent(self, method: str, path: str) -> bool:
        return any(m == method and p == path for m, p, _ in self.bodies)

    # -- transport -------------------------------------------------------
    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content) if request.content else None
        method, path = request.method, request.url.path
        self.bodies.append((method, path, body))

        if request.url.port == 8642:
            return self._gateway(method, path, body)

        if path == "/auth/password-login":
            self.logins += 1
            if body.get("password") != self.password:
                return httpx.Response(401, json={"detail": "Invalid credentials"})
            return httpx.Response(200, json={"ok": True},
                                  headers={"set-cookie": f'hermes_session_at="{self.valid_cookie}"; Path=/'})
        if request.headers.get("cookie") != f'hermes_session_at="{self.valid_cookie}"':
            return httpx.Response(401, json={"detail": "Not authenticated"})

        if (method, path) == ("GET", "/api/model/info"):
            return httpx.Response(200, json={**self.model, "auto_context_length": 0})
        if (method, path) == ("GET", "/api/model/options"):
            return httpx.Response(200, json={
                "model": self.model["model"], "provider": self.model["provider"],
                "providers": [
                    {"slug": "custom", "name": "Custom endpoint", "is_current": True,
                     "models": ["qwen/qwen3.8-flash", "qwen/qwen3.8-plus"]},
                    {"slug": "openrouter", "name": "OpenRouter", "is_current": False, "models": ["a", "b", "c"]},
                ]})
        if (method, path) == ("POST", "/api/model/set"):
            missing = [f for f in ("scope", "provider", "model") if f not in (body or {})]
            if missing:
                return httpx.Response(422, json={"detail": [
                    {"type": "missing", "loc": ["body", f], "msg": "Field required", "input": body} for f in missing]})
            if body["scope"] not in ("main", "auxiliary"):
                return httpx.Response(400, json={"detail": "scope must be 'main' or 'auxiliary'"})
            if self.model_set_response is not None and not body.get("confirm_expensive_model"):
                return httpx.Response(200, json=self.model_set_response)
            self.model = {"model": body["model"], "provider": body["provider"]}
            return httpx.Response(200, json={"ok": True, "scope": "main", "provider": body["provider"],
                                             "model": body["model"]})
        if (method, path) == ("GET", "/api/config"):
            return httpx.Response(200, json=copy.deepcopy(self.config))
        if (method, path) == ("PUT", "/api/config"):
            if not self.ignore_config_writes:
                self.config = _deep_merge(self.config, body["config"])
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/sessions/"):
            # Current published Agent: discover fresh owner fences, never guess zero.
            if method == "GET" and path.endswith("/mutation-snapshot"):
                sid = path[len("/api/sessions/"):-len("/mutation-snapshot")]
                row = self.sessions.get(sid)
                return httpx.Response(200, json={"session_id": sid, "exists": row is not None,
                    "runtime_revision": row.get("runtime_revision", 0) if row is not None else 0,
                    "runtime_generation": row.get("runtime_generation", 0) if row is not None else None})
            sid = path.rsplit("/", 1)[1]
            if method == "DELETE":
                identity = dict(request.url.params)
                if set(identity) != {"request_id", "expected_revision", "expected_generation"}:
                    return httpx.Response(409, json={"detail": "client_upgrade_required"})
                key = (sid, identity["request_id"])
                if key in self.session_receipts:
                    previous, result = self.session_receipts[key]
                    return httpx.Response(200 if identity == previous else 409,
                        json=result if identity == previous else {"detail": "admission_conflict"})
                if sid not in self.sessions:
                    return httpx.Response(200, json={"ok": True, "already_absent": True})
                row = self.sessions[sid]
                if (identity["expected_revision"] != str(row.get("runtime_revision", 0))
                        or identity["expected_generation"] != str(row.get("runtime_generation", 0))):
                    return httpx.Response(409, json={"detail": "revision_conflict"})
                del self.sessions[sid]
                result = {"ok": True, "deleted_ids": [sid]}
                self.session_receipts[key] = (identity, result)
                return httpx.Response(200, json=result)
            if method == "GET":
                if sid not in self.sessions:
                    return httpx.Response(404, json={"detail": "Session not found"})
                return httpx.Response(200, json=self.sessions[sid])
        if path == "/api/webhooks" and method == "POST":
            self.webhooks[body["name"]] = {"prompt": body["prompt"], "enabled": body["enabled"]}
            return httpx.Response(200, json={"name": body["name"], "secret": "s"})
        if path.startswith("/api/webhooks/") and method == "DELETE":
            name = path.rsplit("/", 1)[1]
            if name not in self.webhooks:
                return httpx.Response(404, json={"detail": f"No subscription named '{name}'"})
            del self.webhooks[name]
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/cron/jobs/") and method == "DELETE":
            return httpx.Response(404, json={"detail": "Job not found"})
        if path.startswith("/api/mcp/servers/") and method == "DELETE":
            name = path.rsplit("/", 1)[1]
            return httpx.Response(404, json={"detail": f"Server '{name}' not found"})
        if (method, path) == ("GET", "/api/status"):
            return httpx.Response(200, json={"version": "0.21.6", "gateway_running": True,
                                             "gateway_state": "running", "active_sessions": 1})
        return httpx.Response(404, json={"detail": "Not Found"})

    def _gateway(self, method, path, body):
        if (method, path) != ("POST", "/v1/responses"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        envelope = {
            "id": "resp_abc", "object": "response", "status": "completed",
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "PELICAN-7319"}]}],
        }
        events = [("response.created", {"type": "response.created", "response": {"id": "resp_abc"}}),
                  ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "PELICAN"})]
        if not self.chat_hang:
            events.append(("response.completed", {"type": "response.completed", "response": envelope}))
        if not body.get("stream"):
            return httpx.Response(200, json=envelope)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_EventStream(events, hang=self.chat_hang, owner=self))


@pytest.fixture
def hermes(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_URL", GATEWAY)
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gw-key")
    monkeypatch.setenv("HERMES_DASHBOARD_URL", DASHBOARD)
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "dash-pw")
    monkeypatch.delenv("HERMES_MCP_CHAT_TIMEOUT", raising=False)
    hermes_server._cookie_cache.clear()
    fake = FakeHermes()

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    yield fake
    hermes_server._cookie_cache.clear()


async def _call(tool: str, args: dict):
    async with Client(hermes_server.mcp, mode="legacy") as client:
        return await client.call_tool(tool, args)


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def _tool_description(name: str) -> str:
    async with Client(hermes_server.mcp, mode="legacy") as client:
        tools = (await client.list_tools()).tools
    return next(t for t in tools if t.name == name).description or ""


# ---------------------------------------------------------------------------
# AI-HMCP-001-2：hermes_model set／list_providers
# ---------------------------------------------------------------------------
async def test_model_set_sends_scope_main_and_current_provider(hermes):
    result = await _call("hermes_model", {"action": "set", "model": "qwen/qwen3.8-plus"})

    assert result.is_error is False, _text(result)
    assert hermes.body("POST", "/api/model/set") == {
        "scope": "main", "provider": "custom", "model": "qwen/qwen3.8-plus"}
    assert hermes.model == {"model": "qwen/qwen3.8-plus", "provider": "custom"}


async def test_model_set_with_explicit_provider(hermes):
    result = await _call("hermes_model", {"action": "set", "model": "b", "provider": "openrouter"})

    assert result.is_error is False, _text(result)
    sent = hermes.body("POST", "/api/model/set")
    assert (sent["scope"], sent["provider"], sent["model"]) == ("main", "openrouter", "b")
    assert not hermes.sent("GET", "/api/model/info")   # 有給 provider 就不用查目前值


async def test_model_set_requires_model(hermes):
    result = await _call("hermes_model", {"action": "set", "provider": "openrouter"})

    assert "model" in _text(result)
    assert not hermes.sent("POST", "/api/model/set")


async def test_model_set_422_has_safe_actionable_status(hermes, monkeypatch):
    """Hermes 422 keeps the status and guidance without reflecting upstream body."""
    async def handler(request):
        return httpx.Response(422, json={"detail": [
            {"type": "missing", "loc": ["body", "provider"], "msg": "Field required", "input": {}}]})

    original = hermes.handler

    async def patched(request):
        if request.url.path == "/api/model/set":
            return await handler(request)
        return await original(request)

    hermes.handler = patched
    result = await _call("hermes_model", {"action": "set", "model": "x", "provider": "p"})

    assert result.is_error is True
    text = _text(result)
    assert "BACKEND_HTTP_ERROR status=422" in text
    assert "operation settings" in text and "provider: Field required" not in text
    assert "developer.mozilla.org" not in text


async def test_model_set_expensive_model_confirmation_is_surfaced(hermes):
    hermes.model_set_response = {"ok": False, "scope": "main", "provider": "custom", "model": "big",
                                 "confirm_required": True, "confirm_message": "big costs $75 / 1M tokens"}

    result = await _call("hermes_model", {"action": "set", "model": "big"})

    assert result.is_error is True
    assert "big costs $75 / 1M tokens" in _text(result)
    assert "confirm_expensive" in _text(result)
    assert hermes.model["model"] != "big"

    result = await _call("hermes_model", {"action": "set", "model": "big", "confirm_expensive": True})
    assert result.is_error is False, _text(result)
    assert hermes.model == {"model": "big", "provider": "custom"}


async def test_list_providers_reads_model_options(hermes):
    result = await _call("hermes_model", {"action": "list_providers"})

    assert result.is_error is False, _text(result)
    data = json.loads(_text(result))
    assert [p["slug"] for p in data["providers"]] == ["custom", "openrouter"]
    assert data["providers"][0]["is_current"] is True
    assert data["providers"][1]["total_models"] == 3
    assert data["current"] == {"model": "qwen/qwen3.8-flash", "provider": "custom"}
    assert "minimax" not in _text(result)                 # 不再是寫死清單


# ---------------------------------------------------------------------------
# AI-HMCP-001-4：hermes_config set 點號路徑
# ---------------------------------------------------------------------------
async def test_config_set_dotted_key_writes_nested_value(hermes):
    result = await _call("hermes_config", {"action": "set", "key": "display.compact", "value": "true"})

    assert result.is_error is False, _text(result)
    assert hermes.body("PUT", "/api/config") == {"config": {"display": {"compact": True}}}
    assert hermes.config["display"] == {"compact": True, "skin": "default"}
    assert "display.compact" not in hermes.config            # 不再留下字面鍵
    assert json.loads(_text(result))["value"] is True


async def test_config_set_reports_value_that_did_not_stick(hermes):
    hermes.ignore_config_writes = True

    result = await _call("hermes_config", {"action": "set", "key": "display.compact", "value": "true"})

    assert result.is_error is True
    text = _text(result)
    assert "display.compact" in text and "false" in text.lower()


async def test_config_set_rejects_empty_path_segment(hermes):
    result = await _call("hermes_config", {"action": "set", "key": "display..compact", "value": "1"})

    assert "display..compact" in _text(result)
    assert not hermes.sent("PUT", "/api/config")


# ---------------------------------------------------------------------------
# AI-HMCP-001-5：hermes_chat 的 session_id
# ---------------------------------------------------------------------------
async def test_chat_session_id_maps_to_conversation(hermes):
    result = await _call("hermes_chat", {"message": "Remember PELICAN-7319", "session_id": "qa-hmcp-mem-1016"})

    assert result.is_error is False, _text(result)
    assert _text(result) == "PELICAN-7319"
    sent = hermes.body("POST", "/v1/responses")
    assert sent["conversation"] == "qa-hmcp-mem-1016"
    assert "previous_response_id" not in sent


async def test_chat_response_id_continues_with_previous_response_id(hermes):
    await _call("hermes_chat", {"message": "next", "session_id": "resp_abc"})

    sent = hermes.body("POST", "/v1/responses")
    assert sent["previous_response_id"] == "resp_abc"
    assert "conversation" not in sent


async def test_chat_without_session_id_is_one_off(hermes):
    result = await _call("hermes_chat", {"message": "hi"})

    assert result.is_error is False, _text(result)
    sent = hermes.body("POST", "/v1/responses")
    assert "conversation" not in sent and "previous_response_id" not in sent


async def test_chat_description_documents_session_id(hermes):
    description = await _tool_description("hermes_chat")
    assert "session_id" in description and "conversation" in description


# ---------------------------------------------------------------------------
# AI-HMCP-001-6：hermes_chat 逾時要可讀、可設定，並中止 Hermes 那一輪
# ---------------------------------------------------------------------------
async def test_chat_timeout_closes_stream_without_claiming_remote_cancellation(hermes, monkeypatch):
    monkeypatch.setenv("HERMES_MCP_CHAT_TIMEOUT", "0.3")
    hermes.chat_hang = True

    started = time.monotonic()
    result = await _call("hermes_chat", {"message": "sleep 50"})
    elapsed = time.monotonic() - started

    assert result.is_error is True
    text = _text(result)
    assert "BACKEND_TIMEOUT" in text
    assert "backend cancellation is unconfirmed" in text
    assert "not undone" in text
    assert elapsed < 5
    assert hermes.stream_closed is True


def test_chat_timeout_setting_is_bounded(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_CHAT_TIMEOUT", raising=False)
    assert hermes_server._chat_timeout() == hermes_server.CHAT_TIMEOUT
    assert hermes_server.CHAT_TIMEOUT < 100            # PaaS 經 Cloudflare：單一回應約 100 秒就會被切
    for raw, expected in (("120", 120.0), ("0", 1.0), ("99999", 3600.0), ("abc", hermes_server.CHAT_TIMEOUT),
                          ("nan", hermes_server.CHAT_TIMEOUT), ("inf", hermes_server.CHAT_TIMEOUT)):
        monkeypatch.setenv("HERMES_MCP_CHAT_TIMEOUT", raw)
        assert hermes_server._chat_timeout() == expected, raw


async def test_chat_failed_turn_is_error(hermes, monkeypatch):
    original = hermes._gateway

    def failing(method, path, body):
        envelope = {"id": "resp_x", "status": "failed", "error": {"message": "provider auth failed"}, "output": []}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_EventStream([("response.failed", {"type": "response.failed",
                                                                         "response": envelope})]))

    hermes._gateway = failing
    result = await _call("hermes_chat", {"message": "hi"})
    hermes._gateway = original

    assert result.is_error is True
    assert "provider auth failed" in _text(result)


# ---------------------------------------------------------------------------
# AI-HMCP-001-7：webhook 模板語法
# ---------------------------------------------------------------------------
async def test_webhook_description_uses_hermes_template_syntax(hermes):
    description = await _tool_description("hermes_webhook")
    assert "{{payload}}" not in description
    assert "{__raw__}" in description and "{" in description


async def test_webhook_create_rewrites_legacy_payload_placeholder(hermes):
    result = await _call("hermes_webhook", {"action": "create", "name": "qa", "prompt": "QA {{payload}} / {{ payload }}"})

    assert result.is_error is False, _text(result)
    assert hermes.body("POST", "/api/webhooks")["prompt"] == "QA {__raw__} / {__raw__}"
    assert "{__raw__}" in _text(result)


# ---------------------------------------------------------------------------
# AI-HMCP-001-8：刪除不存在的目標要回 not found
# ---------------------------------------------------------------------------
async def test_session_delete_missing_is_not_found(hermes):
    result = await _call("hermes_session", {"action": "delete", "session_id": "qa-hmcp-no-such-session"})

    assert result.is_error is True
    text = _text(result)
    assert "not found" in text and "qa-hmcp-no-such-session" in text
    assert '"ok": true' not in text


async def test_session_delete_existing_still_works(hermes):
    result = await _call("hermes_session", {"action": "delete", "session_id": "20261016_193839_d56dfbc7"})

    assert result.is_error is False, _text(result)
    assert json.loads(_text(result)) == {"ok": True, "deleted": "20261016_193839_d56dfbc7"}


@pytest.mark.parametrize("revision,generation", [(6, 3), (7, 2)])
async def test_session_delete_wrong_fence_has_no_effect_or_retry(hermes, revision, generation):
    sid = "20261016_193839_d56dfbc7"
    before = copy.deepcopy(hermes.sessions)
    result = await _call("hermes_session", {
        "action": "delete", "session_id": sid, "request_id": "owned-wrong-fence",
        "expected_revision": revision, "expected_generation": generation})

    assert result.is_error is True
    assert "409" in _text(result) and "revision_conflict" in _text(result)
    assert hermes.sessions == before and hermes.session_receipts == {}
    writes = [r for r in hermes.requests if r.method == "DELETE"]
    assert len(writes) == 1
    assert dict(writes[0].url.params) == {
        "request_id": "owned-wrong-fence", "expected_revision": str(revision),
        "expected_generation": str(generation)}


@pytest.mark.parametrize("tool,args,kind", [
    ("hermes_webhook", {"action": "delete", "name": "nope"}, "webhook"),
    ("hermes_cron", {"action": "delete", "job_id": "a1b2c3d4e5f6"}, "cron job"),
    ("hermes_mcp", {"action": "remove", "name": "nope"}, "MCP server"),
])
async def test_delete_missing_resource_is_plain_not_found(hermes, tool, args, kind):
    result = await _call(tool, args)

    assert result.is_error is True
    text = _text(result)
    assert "not found" in text and kind in text
    assert "http://" not in text and "developer.mozilla.org" not in text


# ---------------------------------------------------------------------------
# AI-HMCP-003-1：cookie 遇 401 清除並重登一次；快取鍵含憑證指紋
# ---------------------------------------------------------------------------
async def test_stale_cookie_401_relogs_once_and_retries(hermes):
    assert (await _call("hermes_gateway", {"action": "status"})).is_error is False
    assert hermes.logins == 1

    hermes.valid_cookie = "tok2"           # Hermes 重啟／輪替後舊 cookie 失效
    result = await _call("hermes_gateway", {"action": "status"})

    assert result.is_error is False, _text(result)
    assert hermes.logins == 2
    assert json.loads(_text(result))["running"] is True


async def test_rotated_password_gives_credentials_rejected_hint(hermes):
    assert (await _call("hermes_gateway", {"action": "status"})).is_error is False
    hermes.valid_cookie = "tok2"
    hermes.password = "rotated"            # 247 改了密碼，249 還是舊密碼

    result = await _call("hermes_gateway", {"action": "status"})

    assert result.is_error is True
    text = _text(result)
    assert "憑證被拒" in text and "Connection > Dashboard REST API" in text
    assert hermes.logins == 2              # 只重登一次，不迴圈
    assert hermes_server._cookie_cache == {}


async def test_cookie_cache_key_includes_credential_fingerprint(hermes, monkeypatch):
    assert (await _call("hermes_gateway", {"action": "status"})).is_error is False
    assert hermes.logins == 1

    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "dash-pw-2")
    hermes.password = "dash-pw-2"
    assert (await _call("hermes_gateway", {"action": "status"})).is_error is False

    assert hermes.logins == 2              # 換了憑證就不沿用舊 cookie
    assert all("dash-pw" not in key for key in hermes_server._cookie_cache)


async def test_concurrent_401s_relogin_only_once(hermes):
    """多個 session 同時拿到 401（Hermes 重啟後），只重登一次（不撞 247 每分鐘 10 次的登入限流）。"""
    assert (await _call("hermes_gateway", {"action": "status"})).is_error is False
    hermes.valid_cookie = "tok2"

    results = await asyncio.gather(*(_call("hermes_gateway", {"action": "status"}) for _ in range(8)))

    assert all(r.is_error is False for r in results), [_text(r) for r in results if r.is_error][:1]
    assert hermes.logins == 2


@pytest.mark.parametrize("event,envelope", [
    ("response.incomplete", {"incomplete_details": {"reason": "max_output_tokens"}}),
    ("response.failed", {"error": {"message": "provider unavailable"}}),
    ("response.completed", None),
])
async def test_chat_partial_or_invalid_final_event_is_not_success(hermes, event, envelope):
    def final(method, path, body):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_EventStream([(event, {"response": envelope})]))
    hermes._gateway = final
    result = await _call("hermes_chat", {"message": "hi"})
    assert result.is_error is True, _text(result)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("value", [
    {"secret": "CANARY-denied-secret"},
    {"items": [{"credentials": {"password": "CANARY-denied-secret"}}]},
    {"items": [{"ＡＰＩ＿ＫＥＹ": "CANARY-denied-secret"}]},
])
async def test_config_parent_cannot_bypass_credential_guard(hermes, value, dry_run):
    result = await _call("hermes_config", {"action": "set", "key": "webhooks.signing",
        "value": json.dumps(value), "dry_run": dry_run})
    assert "denied" in _text(result)
    assert "CANARY-denied-secret" not in _text(result)
    assert not hermes.sent("PUT", "/api/config")


async def test_config_parent_patch_preserves_siblings(hermes):
    result = await _call("hermes_config", {"action": "set", "key": "display",
        "value": json.dumps({"compact": True})})
    assert result.is_error is False, _text(result)
    assert json.loads(_text(result))["ok"] is True
    assert hermes.config["display"] == {"compact": True, "skin": "default"}


async def test_config_parent_ignored_patch_is_still_failure(hermes):
    hermes.ignore_config_writes = True
    result = await _call("hermes_config", {"action": "set", "key": "display",
        "value": json.dumps({"compact": True})})
    assert result.is_error is True
    assert hermes.config["display"]["compact"] is False


@pytest.mark.parametrize("name", ["clientSecret", "accessToken"])
@pytest.mark.parametrize("shape", ["direct", "nested_native", "nested_text"])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_config_camel_case_credentials_are_denied(hermes, name, shape, dry_run):
    value = {"items": [{name: "CANARY-camel-secret"}]}
    key = "webhooks.signing"
    if shape == "direct":
        key += "." + name
        value = "CANARY-camel-secret"
    elif shape == "nested_text":
        value = json.dumps(value)
    result = await _call("hermes_config", {"action": "set", "key": key, "value": value, "dry_run": dry_run})
    assert "denied" in _text(result)
    assert "CANARY-camel-secret" not in _text(result)
    assert not hermes.sent("PUT", "/api/config")


@pytest.mark.parametrize("requested,actual", [
    ([True], [1]),
    ([{"enabled": True}], [{"enabled": 1}]),
    ([{"enabled": True}], [{"enabled": True, "extra": 1}]),
])
async def test_config_array_readback_rejects_ignored_or_partial_replacement(hermes, requested, actual):
    hermes.config["display"]["flags"] = actual
    hermes.ignore_config_writes = True
    result = await _call("hermes_config", {"action": "set", "key": "display",
        "value": {"flags": requested}})
    assert result.is_error is True, _text(result)
