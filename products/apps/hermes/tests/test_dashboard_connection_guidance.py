"""Dashboard connection failures keep the HA guidance without exposing its address."""

import asyncio
import json

import httpx
import pytest
from hermes_mcp_server import server as hermes
from mcp.client import Client


@pytest.mark.asyncio
async def test_dashboard_connect_error_is_safe_in_tool_and_inspect(monkeypatch):
    address = "http://127.0.0.1:49199/private-dashboard?token=secret-123"
    monkeypatch.setenv("HERMES_DASHBOARD_URL", address)
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "synthetic-user")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "synthetic-password")

    async def fail_connect(_connection):
        raise httpx.ConnectError(
            f"connection failed for {address} with synthetic-password",
            request=httpx.Request("GET", address),
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", fail_connect)
    expected = (
        "BACKEND_UNAVAILABLE: Hermes Dashboard 不可達 (unreachable): Check that the Hermes service is running "
        "and that Connection > Dashboard REST API in the MCP admin page has the right address."
    )

    async with Client(hermes.mcp, mode="legacy") as client:
        tool = await client.call_tool("hermes_gateway", {"action": "status"})
        assert tool.is_error
        tool_text = "\n".join(c.text for c in tool.content if c.type == "text")
        assert tool_text == f"Error executing tool hermes_gateway: {expected}"

        inspect = await client.call_tool("hermes_inspect", {"target": "status"})
        assert inspect.is_error
        inspect_result = json.loads(inspect.structured_content["result"])
        assert inspect_result["status_error"] == expected

    for text in (tool_text, json.dumps(inspect_result)):
        assert address not in text
        assert "synthetic-password" not in text
        assert "secret-123" not in text


@pytest.mark.asyncio
async def test_missing_dashboard_url_has_same_public_code_as_ha(monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "")
    expected = (
        "BACKEND_UNAVAILABLE: dashboard_url is not configured - set it in the admin GUI "
        "under Connection > Dashboard REST API"
    )
    async with Client(hermes.mcp, mode="legacy") as client:
        tool = await client.call_tool("hermes_gateway", {"action": "status"})
        assert tool.is_error
        assert tool.content[0].text == f"Error executing tool hermes_gateway: {expected}"
        inspect = await client.call_tool("hermes_inspect", {"target": "status"})
        assert inspect.is_error
        assert json.loads(inspect.structured_content["result"])["status_error"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            "forbidden",
            (
                "BACKEND_AUTH_REJECTED status=403: Hermes Dashboard 憑證被拒 (credentials rejected); update "
                "Connection > Dashboard REST API in the MCP admin page and press Save & Restart."
            ),
        ),
        (
            "disconnect",
            (
                "BACKEND_UNAVAILABLE: Hermes Dashboard 不可達 (unreachable): Check that the Hermes service is running "
                "and that Connection > Dashboard REST API in the MCP admin page has the right address."
            ),
        ),
        (
            "redirect",
            (
                "BACKEND_REDIRECT_REJECTED status=307: Hermes Dashboard returned a redirect; verify "
                "Connection > Dashboard REST API in the MCP admin page and press Save & Restart."
            ),
        ),
    ],
)
async def test_dashboard_public_errors_hide_url_and_upstream_text(monkeypatch, failure, expected):
    address = "http://user:private-pass@127.0.0.1:49199/private-path?token=private-query"
    upstream_secret = "upstream-secret-body"
    redirect_secret = "redirect-secret-location"
    monkeypatch.setenv("HERMES_DASHBOARD_URL", address)

    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "forbidden":
            return httpx.Response(403, json={"detail": upstream_secret}, request=request)
        if failure == "redirect":
            return httpx.Response(
                307,
                headers={"Location": f"http://127.0.0.1:49200/{redirect_secret}"},
                json={"detail": upstream_secret},
                request=request,
            )
        raise httpx.RemoteProtocolError(
            f"server disconnected after accept: {address}; {upstream_secret}", request=request
        )

    requests = []

    async def fake_dashboard_client(_connection):
        def record(request):
            requests.append((request.method, request.url.path))
            return respond(request)

        return httpx.AsyncClient(
            base_url=address, transport=httpx.MockTransport(record), follow_redirects=False
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_dashboard_client)
    async with Client(hermes.mcp, mode="legacy") as client:
        tool = await client.call_tool("hermes_gateway", {"action": "status"})
        assert tool.is_error
        tool_text = "\n".join(c.text for c in tool.content if c.type == "text")
        assert expected in tool_text

        inspect = await client.call_tool("hermes_inspect", {"target": "status"})
        assert inspect.is_error
        inspect_result = json.loads(inspect.structured_content["result"])
        assert inspect_result["status_error"] == expected

    assert len(requests) == 2 and all(method == "GET" for method, _ in requests)
    assert all(redirect_secret not in path for _, path in requests)
    for public_text in (tool_text, json.dumps(inspect_result)):
        for secret in (
            address, "private-pass", "private-path", "private-query", upstream_secret, redirect_secret
        ):
            assert secret not in public_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (
            401, "upstream-private-401",
            (
                "BACKEND_AUTH_REJECTED status=401: Hermes Dashboard 憑證被拒 (credentials rejected); update "
                "Connection > Dashboard REST API in the MCP admin page and press Save & Restart."
            ),
        ),
        (
            422, "upstream-private-422",
            (
                "BACKEND_HTTP_ERROR status=422: Hermes Dashboard request failed; verify "
                "Connection > Dashboard REST API in the MCP admin page and the operation settings."
            ),
        ),
        (
            409, "revision_conflict",
            (
                "BACKEND_HTTP_ERROR status=409 code=revision_conflict: Hermes Dashboard rejected the operation; "
                "check the session state and Connection > Dashboard REST API in the MCP admin page."
            ),
        ),
        (
            409, "revision_conflict-upstream-private-409",
            (
                "BACKEND_HTTP_ERROR status=409: Hermes Dashboard request failed; verify "
                "Connection > Dashboard REST API in the MCP admin page and the operation settings."
            ),
        ),
    ],
)
async def test_dashboard_http_status_uses_only_fixed_public_details(monkeypatch, status, detail, expected):
    address = "http://user:private-pass@127.0.0.1:49199/private-path?token=private-query"
    monkeypatch.setenv("HERMES_DASHBOARD_URL", address)

    async def fake_dashboard_client(_connection):
        def respond(request):
            return httpx.Response(status, json={"detail": detail}, request=request)

        return httpx.AsyncClient(base_url="http://127.0.0.1:49199", transport=httpx.MockTransport(respond))

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_dashboard_client)
    async with Client(hermes.mcp, mode="legacy") as client:
        tool = await client.call_tool("hermes_gateway", {"action": "status"})
        assert tool.is_error and expected in tool.content[0].text
        inspect = await client.call_tool("hermes_inspect", {"target": "status"})
        assert inspect.is_error
        parsed = json.loads(inspect.structured_content["result"])
        assert parsed["status_error"] == expected
        assert json.loads(inspect.content[0].text) == parsed

    public_text = tool.content[0].text + inspect.content[0].text
    for secret in (address, "private-pass", "private-path", "private-query", "upstream-private"):
        assert secret not in public_text


@pytest.mark.asyncio
async def test_invalid_dashboard_url_does_not_echo_its_sensitive_value(monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "private-path?token=private-query")
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool("hermes_gateway", {"action": "status"})
    assert result.is_error
    assert "dashboard_url must start with http:// or https://" in result.content[0].text
    assert "private-path" not in result.content[0].text
    assert "private-query" not in result.content[0].text


@pytest.mark.asyncio
async def test_invalid_dashboard_port_is_safe_before_login_in_tool_and_inspect(monkeypatch):
    address = "http://user:private-pass@127.0.0.1:port-secret/private-path?token=private-query"
    monkeypatch.setenv("HERMES_DASHBOARD_URL", address)
    login_attempts = []

    async def fake_login(*args):
        login_attempts.append(args)
        return "synthetic-cookie"

    monkeypatch.setattr(hermes, "_dashboard_login", fake_login)
    expected = (
        "BACKEND_UNAVAILABLE: dashboard_url must be a valid http:// or https:// URL - "
        "fix it in the admin GUI under Connection > Dashboard REST API"
    )

    async with Client(hermes.mcp, mode="legacy") as client:
        tool = await client.call_tool("hermes_gateway", {"action": "status"})
        inspect = await client.call_tool("hermes_inspect", {"target": "status"})

    assert tool.is_error
    assert tool.content[0].text == f"Error executing tool hermes_gateway: {expected}"
    assert inspect.is_error
    parsed = json.loads(inspect.structured_content["result"])
    assert parsed["status_error"] == expected
    assert json.loads(inspect.content[0].text) == parsed
    assert login_attempts == []
    public = json.dumps(
        {"tool": tool.model_dump(mode="json"), "inspect": inspect.model_dump(mode="json")},
        ensure_ascii=False,
    )
    for secret in ("port-secret", "private-pass", "private-path", "private-query"):
        assert secret not in public


@pytest.mark.asyncio
async def test_bad_gzip_response_keeps_safe_tool_and_inspect_errors(monkeypatch):
    address = "http://user:private-pass@127.0.0.1:49199/private-path?token=private-query"
    monkeypatch.setenv("HERMES_DASHBOARD_URL", address)
    requests = []

    async def fake_dashboard_client(_connection):
        def respond(request):
            requests.append((request.method, request.url.path))
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                content=b"synthetic-upstream-body-sentinel",
                request=request,
            )

        return httpx.AsyncClient(
            base_url="http://127.0.0.1:49199", transport=httpx.MockTransport(respond)
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_dashboard_client)
    expected = (
        "BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned an unreadable response; "
        "verify Connection > Dashboard REST API in the MCP admin page."
    )

    async with Client(hermes.mcp, mode="legacy") as client:
        tool = await client.call_tool("hermes_gateway", {"action": "status"})
        inspect = await client.call_tool("hermes_inspect", {"target": "status"})

    assert tool.is_error
    assert tool.content[0].text == f"Error executing tool hermes_gateway: {expected}"
    assert inspect.is_error
    parsed = json.loads(inspect.structured_content["result"])
    assert parsed["status_error"] == expected
    assert json.loads(inspect.content[0].text) == parsed
    assert len(requests) == 2 and all(method == "GET" for method, _ in requests)
    public = json.dumps(
        {"tool": tool.model_dump(mode="json"), "inspect": inspect.model_dump(mode="json")},
        ensure_ascii=False,
    )
    for secret in (
        "private-pass", "private-path", "private-query", "synthetic-upstream-body-sentinel"
    ):
        assert secret not in public


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"\xffsynthetic-body-sentinel", id="invalid-utf8"),
        pytest.param(b"{synthetic-body-sentinel", id="invalid-json"),
        pytest.param(
            b"[" * 16384 + b'"synthetic-body-sentinel"' + b"]" * 16384,
            id="deep-json",
        ),
    ],
)
async def test_invalid_dashboard_json_keeps_safe_tool_and_inspect_errors(monkeypatch, body):
    address = "http://user:private-pass@127.0.0.1:49199/private-path?token=private-query"
    monkeypatch.setenv("HERMES_DASHBOARD_URL", address)
    requests = []

    async def fake_dashboard_client(_connection):
        def respond(request):
            requests.append((request.method, request.url.path))
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=body,
                request=request,
            )

        return httpx.AsyncClient(
            base_url="http://127.0.0.1:49199", transport=httpx.MockTransport(respond)
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_dashboard_client)
    expected = (
        "BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned non-JSON; "
        "verify Connection > Dashboard REST API in the MCP admin page."
    )

    async with Client(hermes.mcp, mode="legacy") as client:
        tool = await client.call_tool("hermes_gateway", {"action": "status"})
        inspect = await client.call_tool("hermes_inspect", {"target": "status"})

    assert tool.is_error
    assert tool.content[0].text == f"Error executing tool hermes_gateway: {expected}"
    assert inspect.is_error
    parsed = json.loads(inspect.structured_content["result"])
    assert parsed["status_error"] == expected
    assert json.loads(inspect.content[0].text) == parsed
    assert len(requests) == 2 and all(method == "GET" for method, _ in requests)
    public = json.dumps(
        {"tool": tool.model_dump(mode="json"), "inspect": inspect.model_dump(mode="json")},
        ensure_ascii=False,
    )
    for secret in (
        "private-pass", "private-path", "private-query", "synthetic-body-sentinel"
    ):
        assert secret not in public


@pytest.mark.parametrize("error", [RuntimeError("local-bug"), asyncio.CancelledError()])
def test_dashboard_json_parser_preserves_local_errors_and_cancellation(monkeypatch, error):
    response = httpx.Response(200, json={"ok": True})

    def fail():
        raise error

    monkeypatch.setattr(response, "json", fail)
    with pytest.raises(type(error)) as raised:
        hermes.errors.dashboard_json(response)
    assert raised.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        pytest.param(
            409,
            b"[" * 16384 + b'"synthetic-body-sentinel"' + b"]" * 16384,
            "BACKEND_HTTP_ERROR status=409: Hermes Dashboard request failed; verify "
            "Connection > Dashboard REST API in the MCP admin page and the operation settings.",
            id="deep-409",
        ),
        pytest.param(
            200,
            b"null",
            "BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned a non-object JSON response; "
            "verify Connection > Dashboard REST API in the MCP admin page.",
            id="null-status",
        ),
    ],
)
async def test_dashboard_json_shape_and_409_detail_are_safe_in_tool_and_inspect(
    monkeypatch, status, body, expected
):
    address = "http://user:private-pass@127.0.0.1:49199/private-path?token=private-query"
    monkeypatch.setenv("HERMES_DASHBOARD_URL", address)
    requests = []

    async def fake_dashboard_client(_connection):
        def respond(request):
            requests.append((request.method, request.url.path))
            return httpx.Response(
                status,
                headers={"Content-Type": "application/json"},
                content=body,
                request=request,
            )

        return httpx.AsyncClient(
            base_url="http://127.0.0.1:49199", transport=httpx.MockTransport(respond)
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_dashboard_client)
    async with Client(hermes.mcp, mode="legacy") as client:
        tool = await client.call_tool("hermes_gateway", {"action": "status"})
        inspect = await client.call_tool("hermes_inspect", {"target": "status"})

    assert tool.is_error
    assert tool.content[0].text == f"Error executing tool hermes_gateway: {expected}"
    assert inspect.is_error
    parsed = json.loads(inspect.structured_content["result"])
    assert parsed["status_error"] == expected
    assert json.loads(inspect.content[0].text) == parsed
    assert len(requests) == 2 and all(method == "GET" for method, _ in requests)
    public = json.dumps(
        {"tool": tool.model_dump(mode="json"), "inspect": inspect.model_dump(mode="json")},
        ensure_ascii=False,
    )
    for secret in (
        "private-pass", "private-path", "private-query", "synthetic-body-sentinel"
    ):
        assert secret not in public
