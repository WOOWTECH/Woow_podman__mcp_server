"""Actual MCP calls against malformed Dashboard reply shapes and cron write ambiguity."""

import json

import httpx
import pytest
from mcp.client import Client

from hermes_mcp_server import server as hermes


HINT = "verify Connection > Dashboard REST API in the MCP admin page."
NESTED_SHAPE = f"BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned an unexpected JSON shape; {HINT}"
OBJECT_SHAPE = f"BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned a non-object JSON response; {HINT}"
CRON_UNKNOWN = (
    "CRON_CREATE_OUTCOME_UNKNOWN: Hermes cron create outcome is unknown. "
    "No automatic retry was made. List scheduled jobs in Hermes before deciding to retry."
)
SENTINEL = "synthetic-body-sentinel"


def _install(monkeypatch, *, body, status=200, path, method="GET", transport_error=False,
             post_bodies=None):
    requests = []
    effects = []
    monkeypatch.setenv(
        "HERMES_DASHBOARD_URL",
        "http://user:private-pass@dashboard.synthetic.invalid/private-path?token=private-query",
    )
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "synthetic-user")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "synthetic-password")

    def respond(request):
        requests.append((request.method, request.url.path))
        assert (request.method, request.url.path) == (method, path)
        if post_bodies is not None and request.method == "POST":
            post_bodies.append(json.loads(request.content))
        if request.method == "POST" and (status < 300 or status >= 500):
            effects.append("synthetic-cron-created")
        if transport_error:
            raise httpx.ReadTimeout("synthetic-body-sentinel", request=request)
        return httpx.Response(status, content=json.dumps(body).encode(), request=request)

    async def fake_client(_connection):
        return httpx.AsyncClient(
            base_url="http://dashboard.synthetic.invalid",
            transport=httpx.MockTransport(respond),
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_client)
    return requests, effects


def _public(result):
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "path", "body", "expected"),
    [
        ("hermes_webhook", {"action": "list"}, "/api/webhooks", {"subscriptions": None, "private": SENTINEL}, NESTED_SHAPE),
        ("hermes_webhook", {"action": "list"}, "/api/webhooks", {"subscriptions": [{"prompt": 42, "private": SENTINEL}]}, NESTED_SHAPE),
        ("hermes_model", {"action": "list_providers"}, "/api/model/options", {"providers": [{"models": 42, "private": SENTINEL}]}, NESTED_SHAPE),
        ("hermes_model", {"action": "list_providers"}, "/api/model/options", {"providers": None, "private": SENTINEL}, NESTED_SHAPE),
        ("hermes_model", {"action": "list_providers"}, "/api/model/options", {"providers": [{"models": [{"private": SENTINEL}]}]}, NESTED_SHAPE),
        ("hermes_cron", {"action": "list"}, "/api/cron/jobs", [{"prompt": 42, "private": SENTINEL}], NESTED_SHAPE),
        ("hermes_cron", {"action": "list"}, "/api/cron/jobs", {"jobs": [], "private": SENTINEL}, NESTED_SHAPE),
        ("hermes_cron", {"action": "list"}, "/api/cron/jobs", [{"schedule": {"expr": {"private": SENTINEL}}}], NESTED_SHAPE),
        ("hermes_skill", {"action": "list"}, "/api/skills", {"skills": [], "private": SENTINEL}, NESTED_SHAPE),
        ("hermes_session", {"action": "list"}, "/api/sessions", {"sessions": None, "private": SENTINEL}, NESTED_SHAPE),
        ("hermes_session", {"action": "list"}, "/api/sessions", [SENTINEL], OBJECT_SHAPE),
        ("hermes_model", {"action": "info"}, "/api/model/info", None, OBJECT_SHAPE),
        ("hermes_config", {"action": "get"}, "/api/config", None, OBJECT_SHAPE),
    ],
)
async def test_malformed_read_shapes_are_fixed_and_do_not_echo_private_data(
    monkeypatch, tool, args, path, body, expected
):
    requests, effects = _install(monkeypatch, body=body, path=path)
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(tool, args)
    assert result.is_error
    assert result.content[0].text == f"Error executing tool {tool}: {expected}"
    assert requests == [("GET", path)]
    assert effects == []
    public = _public(result)
    for secret in (SENTINEL, "private-pass", "private-path", "private-query"):
        assert secret not in public


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "path", "body", "expected"),
    [
        ("hermes_webhook", {"action": "list"}, "/api/webhooks", {"enabled": True, "base_url": "https://example.invalid", "subscriptions": [{"name": "hook", "enabled": True, "prompt": "hello"}]}, {"platform_enabled": True, "base_url": "https://example.invalid", "total": 1, "webhooks": [{"name": "hook", "enabled": True, "prompt": "hello"}]}),
        ("hermes_model", {"action": "list_providers"}, "/api/model/options", {"model": "m", "provider": "p", "providers": [{"slug": "p", "name": "Provider", "is_current": True, "models": ["m"]}]}, {"current": {"model": "m", "provider": "p"}, "providers": [{"slug": "p", "name": "Provider", "is_current": True, "total_models": 1, "models": ["m"]}]}),
        ("hermes_cron", {"action": "list"}, "/api/cron/jobs", [{"id": "cron-1", "name": "daily", "schedule": {"expr": "0 9 * * *"}, "prompt": "hello", "enabled": True, "state": "idle", "next_run_at": None}], {"total": 1, "jobs": [{"id": "cron-1", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello", "enabled": True, "state": "idle", "next_run": None}]}),
        ("hermes_skill", {"action": "list"}, "/api/skills", [{"name": "skill", "enabled": True, "category": "utility"}], {"total": 1, "skills": [{"name": "skill", "enabled": True, "category": "utility"}]}),
        ("hermes_session", {"action": "list"}, "/api/sessions", {"total": 1, "sessions": [{"id": "session-1"}]}, {"total": 1, "sessions": [{"id": "session-1"}]}),
    ],
)
async def test_valid_read_shapes_keep_existing_projection(monkeypatch, tool, args, path, body, expected):
    requests, effects = _install(monkeypatch, body=body, path=path)
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(tool, args)
    assert not result.is_error
    assert json.loads(result.content[0].text) == expected
    assert requests == [("GET", path)]
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("id", "name", "state", "next_run_at"))
async def test_cron_list_rejects_bool_in_string_or_integer_fields(monkeypatch, field):
    job = {
        "id": "cron-1", "name": "daily", "state": "idle", "next_run_at": None,
        "private": SENTINEL,
    }
    job[field] = True
    requests, effects = _install(monkeypatch, body=[job], path="/api/cron/jobs")
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool("hermes_cron", {"action": "list"})
    assert result.is_error
    assert result.content[0].text == f"Error executing tool hermes_cron: {NESTED_SHAPE}"
    assert requests == [("GET", "/api/cron/jobs")]
    assert effects == []
    assert SENTINEL not in _public(result)


@pytest.mark.asyncio
async def test_cron_list_keeps_valid_integer_id_and_next_run(monkeypatch):
    requests, effects = _install(
        monkeypatch,
        body=[{"id": 0, "name": "daily", "state": "idle", "next_run_at": 123}],
        path="/api/cron/jobs",
    )
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool("hermes_cron", {"action": "list"})
    assert not result.is_error
    assert json.loads(result.content[0].text) == {
        "total": 1,
        "jobs": [{
            "id": 0, "name": "daily", "schedule": None, "prompt": "",
            "enabled": None, "state": "idle", "next_run": 123,
        }],
    }
    assert requests == [("GET", "/api/cron/jobs")]
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "transport_error"),
    [
        (None, 200, False),
        ({"id": {"private": SENTINEL}}, 200, False),
        ({"id": "cron-1"}, 503, False),
        (None, 200, True),
    ],
)
async def test_cron_create_after_one_post_has_unknown_outcome_without_retry(
    monkeypatch, body, status, transport_error
):
    requests, effects = _install(
        monkeypatch, body=body, status=status, path="/api/cron/jobs",
        method="POST", transport_error=transport_error,
    )
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(
            "hermes_cron",
            {"action": "create", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello"},
        )
    assert result.is_error
    assert result.content[0].text == f"Error executing tool hermes_cron: {CRON_UNKNOWN}"
    assert requests == [("POST", "/api/cron/jobs")]
    assert effects == ["synthetic-cron-created"]
    assert SENTINEL not in _public(result)


@pytest.mark.asyncio
async def test_cron_create_success_keeps_existing_result(monkeypatch):
    requests, effects = _install(
        monkeypatch,
        body={"id": "cron-1", "name": "daily", "schedule": {"expr": "0 9 * * *"}, "next_run_at": None},
        path="/api/cron/jobs", method="POST",
    )
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(
            "hermes_cron",
            {"action": "create", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello"},
        )
    assert not result.is_error
    assert json.loads(result.content[0].text) == {
        "ok": True, "id": "cron-1", "name": "daily", "schedule": "0 9 * * *", "next_run": None,
    }
    assert requests == [("POST", "/api/cron/jobs")]
    assert effects == ["synthetic-cron-created"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled_arg", "expected_enabled"),
    [(False, False), (True, True), (None, True)],
)
async def test_cron_create_sends_consistent_pause_flags(monkeypatch, enabled_arg, expected_enabled):
    post_bodies = []
    requests, effects = _install(
        monkeypatch,
        body={"id": "cron-1", "name": "daily", "schedule": {"expr": "0 9 * * *"}},
        path="/api/cron/jobs", method="POST", post_bodies=post_bodies,
    )
    arguments = {
        "action": "create", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello",
    }
    if enabled_arg is not None:
        arguments["enabled"] = enabled_arg
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool("hermes_cron", arguments)
    assert not result.is_error
    assert json.loads(result.content[0].text)["id"] == "cron-1"
    assert requests == [("POST", "/api/cron/jobs")]
    assert effects == ["synthetic-cron-created"]
    assert post_bodies == [{
        "name": "daily", "schedule": "0 9 * * *", "prompt": "hello",
        "paused": not expected_enabled, "enabled": expected_enabled,
    }]


@pytest.mark.asyncio
async def test_cron_create_strict_legacy_schema_rejects_without_unsafe_retry(monkeypatch):
    # If an older endpoint rejects the new field, do not fall back to a
    # second POST that could briefly create an active job.
    posted = []
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "http://dashboard.synthetic.invalid")
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "synthetic-user")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "synthetic-password")

    def respond(request):
        posted.append(json.loads(request.content))
        if "paused" in posted[-1]:
            return httpx.Response(422, json={"detail": "extra_forbidden"}, request=request)
        return httpx.Response(200, json={"id": "legacy-created"}, request=request)

    async def fake_client(_connection):
        return httpx.AsyncClient(
            base_url="http://dashboard.synthetic.invalid",
            transport=httpx.MockTransport(respond),
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_client)
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool("hermes_cron", {
            "action": "create", "name": "daily", "schedule": "0 9 * * *",
            "prompt": "hello", "enabled": False,
        })
    assert result.is_error
    assert "status=422" in result.content[0].text
    assert posted == [{
        "name": "daily", "schedule": "0 9 * * *", "prompt": "hello",
        "paused": True, "enabled": False,
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("next_run_at", (True, False))
async def test_cron_create_bool_next_run_has_unknown_outcome_without_retry(monkeypatch, next_run_at):
    requests, effects = _install(
        monkeypatch,
        body={"id": "cron-1", "next_run_at": next_run_at, "private": SENTINEL},
        path="/api/cron/jobs", method="POST",
    )
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(
            "hermes_cron",
            {"action": "create", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello"},
        )
    assert result.is_error
    assert result.content[0].text == f"Error executing tool hermes_cron: {CRON_UNKNOWN}"
    assert requests == [("POST", "/api/cron/jobs")]
    assert effects == ["synthetic-cron-created"]
    assert SENTINEL not in _public(result)


@pytest.mark.asyncio
async def test_cron_create_keeps_valid_integer_id_and_next_run(monkeypatch):
    requests, effects = _install(
        monkeypatch,
        body={"id": 0, "next_run_at": 123},
        path="/api/cron/jobs", method="POST",
    )
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(
            "hermes_cron",
            {"action": "create", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello"},
        )
    assert not result.is_error
    assert json.loads(result.content[0].text) == {
        "ok": True, "id": 0, "name": None, "schedule": "0 9 * * *", "next_run": 123,
    }
    assert requests == [("POST", "/api/cron/jobs")]
    assert effects == ["synthetic-cron-created"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"detail": SENTINEL}, "BACKEND_AUTH_REJECTED status=401"),
        (307, {"detail": SENTINEL}, "BACKEND_REDIRECT_REJECTED status=307"),
        (409, {"detail": "revision_conflict", "private": SENTINEL}, "BACKEND_HTTP_ERROR status=409 code=revision_conflict"),
    ],
)
async def test_cron_create_refusals_keep_fixed_categories_and_one_post(monkeypatch, status, body, expected):
    requests, effects = _install(monkeypatch, body=body, status=status, path="/api/cron/jobs", method="POST")
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(
            "hermes_cron",
            {"action": "create", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello"},
        )
    assert result.is_error and expected in result.content[0].text
    assert requests == [("POST", "/api/cron/jobs")]
    assert effects == []
    assert SENTINEL not in _public(result)


@pytest.mark.asyncio
async def test_cron_create_401_does_not_relogin_or_resend(monkeypatch):
    requests = []
    logins = []

    def respond(request):
        requests.append(request.method)
        return httpx.Response(401, json={"detail": SENTINEL}, request=request)

    async def relogin():
        logins.append(True)
        return "refreshed-cookie"

    async def fake_client(_connection):
        client = httpx.AsyncClient(
            base_url="http://dashboard.synthetic.invalid",
            transport=httpx.MockTransport(respond),
        )
        return hermes._relogin_on_401(client, relogin)

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_client)
    async with Client(hermes.mcp, mode="legacy") as client:
        result = await client.call_tool(
            "hermes_cron",
            {"action": "create", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello"},
        )
    assert result.is_error and "BACKEND_AUTH_REJECTED status=401" in result.content[0].text
    assert requests == ["POST"] and logins == []
