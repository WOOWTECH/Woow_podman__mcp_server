"""hermes_cron enabled: create paused in one write, the fallback for a Hermes that ignores paused,
and update through pause/resume. Actual MCP calls against a recorded synthetic Dashboard."""

import json

import httpx
import pytest
from mcp.client import Client

from hermes_mcp_server import server as hermes


CREATE = ("POST", "/api/cron/jobs")
PAUSE = ("POST", "/api/cron/jobs/c1/pause")
RESUME = ("POST", "/api/cron/jobs/c1/resume")
PUT = ("PUT", "/api/cron/jobs/c1")
DELETE = ("DELETE", "/api/cron/jobs/c1")
JOB = {"id": "c1", "name": "daily", "schedule": {"expr": "0 9 * * *"}}
CREATE_ARGS = {"action": "create", "name": "daily", "schedule": "0 9 * * *", "prompt": "hello"}
TIMEOUT = "timeout"
CRON_UNKNOWN = (
    "CRON_CREATE_OUTCOME_UNKNOWN: Hermes cron create outcome is unknown. "
    "No automatic retry was made. List scheduled jobs in Hermes before deciding to retry."
)


def _install(monkeypatch, routes):
    """Answer each (method, path) from *routes*; record (method, path, JSON body) of every request."""
    calls = []
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "http://dashboard.synthetic.invalid")
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "synthetic-user")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "synthetic-password")

    def respond(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        answer = routes[(request.method, request.url.path)]
        if answer == TIMEOUT:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        status, payload = answer
        return httpx.Response(status, content=json.dumps(payload).encode(), request=request)

    async def fake_client(_connection):
        return httpx.AsyncClient(
            base_url="http://dashboard.synthetic.invalid",
            transport=httpx.MockTransport(respond),
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", fake_client)
    return calls


async def _cron(arguments):
    async with Client(hermes.mcp, mode="legacy") as client:
        return await client.call_tool("hermes_cron", arguments)


def _route(calls):
    return [(method, path) for method, path, _ in calls]


@pytest.mark.asyncio
async def test_create_disabled_sends_paused_once_and_reports_it(monkeypatch):
    calls = _install(monkeypatch, {
        CREATE: (200, {**JOB, "enabled": False, "state": "paused", "next_run_at": None}),
    })
    result = await _cron({**CREATE_ARGS, "enabled": False})
    assert not result.is_error
    assert json.loads(result.content[0].text) == {
        "ok": True, "id": "c1", "name": "daily", "schedule": "0 9 * * *", "enabled": False, "next_run": None,
    }
    assert calls == [("POST", "/api/cron/jobs", {
        "name": "daily", "schedule": "0 9 * * *", "prompt": "hello", "paused": True, "enabled": False,
    })]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ({}, {"enabled": True}))
async def test_create_enabled_sends_paused_false_and_makes_no_follow_up(monkeypatch, extra):
    calls = _install(monkeypatch, {
        CREATE: (200, {**JOB, "enabled": True, "state": "scheduled", "next_run_at": "2027-01-01T09:00:00"}),
    })
    result = await _cron({**CREATE_ARGS, **extra})
    assert not result.is_error
    assert json.loads(result.content[0].text)["enabled"] is True
    assert calls == [("POST", "/api/cron/jobs", {
        "name": "daily", "schedule": "0 9 * * *", "prompt": "hello", "paused": False, "enabled": True,
    })]


@pytest.mark.asyncio
@pytest.mark.parametrize("created", (
    {**JOB, "enabled": True, "next_run_at": "2027-01-01T09:00:00"},
    {**JOB, "next_run_at": "2027-01-01T09:00:00"},
))
async def test_create_disabled_on_a_hermes_that_ignores_paused_pauses_the_job(monkeypatch, created):
    calls = _install(monkeypatch, {
        CREATE: (200, created),
        PAUSE: (200, {**JOB, "enabled": False, "state": "paused", "next_run_at": "2027-01-01T09:00:00"}),
    })
    result = await _cron({**CREATE_ARGS, "enabled": False})
    assert not result.is_error
    assert json.loads(result.content[0].text)["enabled"] is False
    assert _route(calls) == [CREATE, PAUSE]


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", (
    (500, {"detail": "synthetic failure"}),
    TIMEOUT,
    (200, {**JOB, "enabled": True}),
    (404, {"detail": "Job not found"}),
))
async def test_create_disabled_that_cannot_be_paused_is_deleted(monkeypatch, pause):
    calls = _install(monkeypatch, {
        CREATE: (200, {**JOB, "enabled": True}),
        PAUSE: pause,
        DELETE: (200, {"ok": True}),
    })
    result = await _cron({**CREATE_ARGS, "enabled": False})
    assert result.is_error
    assert "CRON_CREATE_NOT_DISABLED:" in result.content[0].text
    assert "nothing is scheduled" in result.content[0].text
    assert _route(calls) == [CREATE, PAUSE, DELETE]


@pytest.mark.asyncio
async def test_create_disabled_already_gone_on_delete_still_reports_not_disabled(monkeypatch):
    calls = _install(monkeypatch, {
        CREATE: (200, {**JOB, "enabled": True}),
        PAUSE: (500, {"detail": "synthetic failure"}),
        DELETE: (404, {"detail": "Job not found"}),
    })
    result = await _cron({**CREATE_ARGS, "enabled": False})
    assert result.is_error
    assert "CRON_CREATE_NOT_DISABLED:" in result.content[0].text
    assert _route(calls) == [CREATE, PAUSE, DELETE]


@pytest.mark.asyncio
@pytest.mark.parametrize("delete", ((500, {"detail": "synthetic failure"}), TIMEOUT))
async def test_create_disabled_left_enabled_names_the_job_and_never_retries(monkeypatch, delete):
    calls = _install(monkeypatch, {
        CREATE: (200, {**JOB, "enabled": True}),
        PAUSE: (500, {"detail": "synthetic failure"}),
        DELETE: delete,
    })
    result = await _cron({**CREATE_ARGS, "enabled": False})
    assert result.is_error
    assert "CRON_CREATE_LEFT_ENABLED:" in result.content[0].text
    assert "'c1'" in result.content[0].text
    assert _route(calls) == [CREATE, PAUSE, DELETE]


@pytest.mark.asyncio
@pytest.mark.parametrize("create", ((503, {"id": "c1"}), TIMEOUT, (200, {"id": {"bad": True}})))
async def test_create_disabled_with_unknown_outcome_makes_no_follow_up_request(monkeypatch, create):
    calls = _install(monkeypatch, {CREATE: create})
    result = await _cron({**CREATE_ARGS, "enabled": False})
    assert result.is_error
    assert result.content[0].text == f"Error executing tool hermes_cron: {CRON_UNKNOWN}"
    assert _route(calls) == [CREATE]


@pytest.mark.asyncio
async def test_create_rejects_a_non_boolean_enabled_reply_as_unknown_outcome(monkeypatch):
    calls = _install(monkeypatch, {CREATE: (200, {**JOB, "enabled": "false"})})
    result = await _cron({**CREATE_ARGS, "enabled": False})
    assert result.is_error
    assert result.content[0].text == f"Error executing tool hermes_cron: {CRON_UNKNOWN}"
    assert _route(calls) == [CREATE]


@pytest.mark.asyncio
@pytest.mark.parametrize(("extra", "route"), (
    ({"enabled": False}, [PAUSE]),
    ({"enabled": True}, [RESUME]),
    ({"prompt": "new", "enabled": False}, [PAUSE, PUT]),
    ({"prompt": "new", "enabled": True}, [PUT, RESUME]),
    ({"prompt": "new"}, [PUT]),
))
async def test_update_changes_the_run_state_only_through_pause_and_resume(monkeypatch, extra, route):
    calls = _install(monkeypatch, {
        PAUSE: (200, {**JOB, "enabled": False, "state": "paused"}),
        RESUME: (200, {**JOB, "enabled": True, "state": "scheduled"}),
        PUT: (200, {**JOB, "prompt": "new"}),
    })
    result = await _cron({"action": "update", "job_id": "c1", **extra})
    assert not result.is_error
    reply = json.loads(result.content[0].text)
    assert reply["ok"] is True and reply["updated"] == "c1"
    assert reply.get("enabled") == extra.get("enabled")
    assert _route(calls) == route
    for method, _path, body in calls:
        if method == "PUT":
            assert body == {"updates": {"prompt": "new"}}


@pytest.mark.asyncio
async def test_update_stops_before_changing_fields_when_pause_fails(monkeypatch):
    calls = _install(monkeypatch, {
        PAUSE: (500, {"detail": "synthetic failure"}),
        PUT: (200, JOB),
    })
    result = await _cron({"action": "update", "job_id": "c1", "prompt": "new", "enabled": False})
    assert result.is_error
    assert _route(calls) == [PAUSE]


@pytest.mark.asyncio
async def test_update_without_any_field_names_enabled_too(monkeypatch):
    calls = _install(monkeypatch, {})
    result = await _cron({"action": "update", "job_id": "c1"})
    assert json.loads(result.content[0].text) == {
        "error": "provide at least one field to update (prompt, schedule, name, enabled)",
    }
    assert calls == []
