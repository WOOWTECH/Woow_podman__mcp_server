"""Every MCP-visible Hermes error reads ``CODE: text`` (PaaS and HA add-on carry the same code table)."""

import json

import httpx
import pytest
from mcp.client import Client

from hermes_mcp_server import server as hermes

SENTINEL = "synthetic-upstream-sentinel"
GATEWAY_URL = "http://gateway-user:gateway-pass@gateway.synthetic.invalid:8642/private-gateway-path"


def _text(result):
    return result.content[0].text.removeprefix(f"Error executing tool {_text.tool}: ")


def _env(monkeypatch, dashboard_handler=None, gateway_handler=None):
    monkeypatch.setenv("HERMES_GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "synthetic-gateway-key")
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "http://dashboard.synthetic.invalid")
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "synthetic-user")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "synthetic-password")

    async def dashboard_client(_connection):
        return httpx.AsyncClient(base_url="http://dashboard.synthetic.invalid",
                                 transport=httpx.MockTransport(dashboard_handler))

    def gateway_client(_url, _key, timeout=30.0):
        return httpx.AsyncClient(base_url="http://gateway.synthetic.invalid",
                                 transport=httpx.MockTransport(gateway_handler), timeout=timeout)

    if dashboard_handler:
        monkeypatch.setattr(hermes, "_get_dashboard_client", dashboard_client)
    if gateway_handler:
        monkeypatch.setattr(hermes, "_gateway_client", gateway_client)


async def _call(tool, args):
    _text.tool = tool
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(tool, args)
    assert result.is_error
    return _text(result)


def _json(request, status, body):
    return httpx.Response(status, json=body, request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "args", "code"), [
    ("hermes_cron", {"action": "delete", "job_id": "job-x"}, "NOT_FOUND"),
    ("hermes_session", {"action": "get", "session_id": "gone"}, "NOT_FOUND"),
    ("hermes_webhook", {"action": "delete", "name": "gone"}, "NOT_FOUND"),
    ("hermes_mcp", {"action": "remove", "name": "gone"}, "NOT_FOUND"),
])
async def test_missing_resource_is_not_found(monkeypatch, tool, args, code):
    _env(monkeypatch, lambda request: _json(request, 404, {"detail": "No such thing"}))
    text = await _call(tool, args)
    assert text.startswith(f"{code}: Hermes ") and "not found (不存在)" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "args"), [
    ("hermes_mcp", {"action": "add", "name": "qa", "url": "stdio:cmd"}),
    ("hermes_mcp", {"action": "add", "name": "qa", "url": "http://localhost:9/mcp"}),
    ("hermes_mcp", {"action": "add", "name": "qa", "url": "http://127.0.0.1:9/mcp"}),
    ("hermes_mcp", {"action": "remove", "name": "../x"}),
    ("hermes_session", {"action": "get", "session_id": "../config/raw"}),
    ("hermes_session", {"action": "get", "session_id": "ok", "request_id": "r1"}),
    ("hermes_session", {"action": "delete", "session_id": "ok", "request_id": "r1"}),
])
async def test_input_guards_are_invalid_input(monkeypatch, tool, args):
    _env(monkeypatch, lambda request: pytest.fail("no backend request expected"))
    text = await _call(tool, args)
    assert text.startswith("INVALID_INPUT: "), text


@pytest.mark.asyncio
async def test_loopback_mcp_url_has_one_wording(monkeypatch):
    _env(monkeypatch, lambda request: pytest.fail("no backend request expected"))
    for url in ("http://localhost:9/mcp", "http://127.0.0.1:9/mcp", "http://[::1]:9/mcp"):
        text = await _call("hermes_mcp", {"action": "add", "name": "qa", "url": url})
        assert text == "INVALID_INPUT: loopback MCP servers (localhost, 127.0.0.0/8, ::1) cannot be added"


@pytest.mark.asyncio
async def test_expensive_model_needs_confirmation(monkeypatch):
    def handler(request):
        if request.url.path == "/api/model/set":
            return _json(request, 200, {"confirm_required": True, "confirm_message": "Costly."})
        return _json(request, 200, {"provider": "p"})

    _env(monkeypatch, handler)
    text = await _call("hermes_model", {"action": "set", "model": "m", "provider": "p"})
    assert text.startswith("CONFIRMATION_REQUIRED: Hermes did not switch the model: Costly.")


@pytest.mark.asyncio
async def test_config_write_not_applied(monkeypatch):
    def handler(request):
        if request.method == "GET":
            return _json(request, 200, {"display": {"x": 1}})
        return _json(request, 200, {"ok": True})

    _env(monkeypatch, handler)
    text = await _call("hermes_config", {"action": "set", "key": "display.x", "value": "2"})
    assert text.startswith("CONFIG_NOT_APPLIED: Hermes accepted the write but 'display.x'"), text


@pytest.mark.asyncio
@pytest.mark.parametrize(("sse", "expected"), [
    ("event: response.created\ndata: {}\n\n", "BACKEND_UNEXPECTED_RESPONSE: Hermes Gateway ended the reply"),
    ('event: response.failed\ndata: {"response": {"status": "failed", "error": {"message": "boom"}}}\n\n',
     "CHAT_TURN_FAILED: Hermes could not complete the turn: boom"),
])
async def test_chat_gateway_internal_errors(monkeypatch, sse, expected):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode(),
                              request=request)

    _env(monkeypatch, None, handler)
    assert (await _call("hermes_chat", {"message": "hi"})).startswith(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "args"), [
    ("hermes_chat", {"message": "hi"}),
    ("hermes_inspect", {"target": "capabilities"}),
])
async def test_gateway_403_is_fixed_safe_wording(monkeypatch, tool, args):
    _env(monkeypatch, lambda request: _json(request, 200, {}),
         lambda request: _json(request, 403, {"detail": SENTINEL}))
    text = await _call(tool, args)
    if tool == "hermes_inspect":
        text = json.loads(text)["capabilities_error"]
    assert text == (
        "BACKEND_AUTH_REJECTED status=403: Hermes Gateway 憑證被拒 (credentials rejected); "
        "update Connection > Gateway API Server in the MCP admin page and press Save & Restart."
    )
    for private in (SENTINEL, "gateway.synthetic.invalid", "gateway-pass", "private-gateway-path"):
        assert private not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "tool", "args"), [
    ([], "hermes_config", {"action": "get"}),
    ([], "hermes_model", {"action": "info"}),
    ([], "hermes_session", {"action": "list"}),
    ({"x": 1}, "hermes_skill", {"action": "list"}),
    ([{"name": 7}], "hermes_skill", {"action": "list"}),
])
async def test_wrong_dashboard_shapes_are_unexpected_response(monkeypatch, body, tool, args):
    _env(monkeypatch, lambda request: _json(request, 200, body))
    text = await _call(tool, args)
    assert text.startswith("BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned a"), text
