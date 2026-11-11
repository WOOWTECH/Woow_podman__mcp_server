"""Real MCP tool calls against the published Agent's fenced-delete HTTP contract."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from mcp.client import Client

from hermes_mcp_server import server as hermes

SID = "owned-native-session"


class Dashboard:
    def __init__(self):
        self.exists = True
        self.revision = 7
        self.generation = 3
        self.requests = []
        self.receipts = {}
        self.effects = 0
        self.snapshot_override = None
        self.snapshot_status = 200
        self.delete_status = None
        self.lose_reply = False
        self.race = False

    def handler(self, request):
        self.requests.append(
            (request.method, request.url.path, dict(request.url.params))
        )
        if (
            request.method == "GET"
            and request.url.path == f"/api/sessions/{SID}/mutation-snapshot"
        ):
            if self.snapshot_status != 200:
                return httpx.Response(
                    self.snapshot_status, json={"detail": "Not Found"}
                )
            value = (
                self.snapshot_override
                if self.snapshot_override is not None
                else {
                    "session_id": SID,
                    "exists": self.exists,
                    "runtime_revision": self.revision if self.exists else 0,
                    "runtime_generation": self.generation if self.exists else None,
                }
            )
            return httpx.Response(200, json=value)
        assert request.method == "DELETE" and request.url.path == f"/api/sessions/{SID}"
        p = dict(request.url.params)
        if set(p) != {"request_id", "expected_revision", "expected_generation"}:
            return httpx.Response(409, json={"detail": "client_upgrade_required"})
        key = p["request_id"]
        identity = (p["expected_revision"], p["expected_generation"])
        if key in self.receipts:
            old, result = self.receipts[key]
            return httpx.Response(
                200 if identity == old else 409,
                json=result if identity == old else {"detail": "admission_conflict"},
            )
        if self.delete_status:
            return httpx.Response(
                self.delete_status, json={"detail": "fixture_refusal"}
            )
        if self.race:
            self.revision += 1
        if not self.exists:
            return httpx.Response(200, json={"ok": True, "already_absent": True})
        if identity != (str(self.revision), str(self.generation)):
            return httpx.Response(409, json={"detail": "revision_conflict"})
        self.exists = False
        self.effects += 1
        result = {"ok": True, "deleted_ids": [SID]}
        self.receipts[key] = (identity, result)
        if self.lose_reply:
            raise httpx.ReadTimeout("reply lost after commit", request=request)
        return httpx.Response(200, json=result)


@pytest.fixture
def dashboard(monkeypatch):
    backend = Dashboard()

    async def client(_):
        return httpx.AsyncClient(
            base_url="http://fixture.invalid",
            transport=httpx.MockTransport(backend.handler),
        )

    monkeypatch.setattr(hermes, "_get_dashboard_client", client)
    monkeypatch.setattr(
        hermes, "_get_connection", lambda: {"dashboard_url": "http://fixture.invalid"}
    )
    return backend


@pytest.mark.asyncio
async def test_default_delete_reads_owner_snapshot_and_passes_fences(dashboard):
    result = json.loads(await hermes.hermes_session(action="delete", session_id=SID))
    assert result == {"ok": True, "deleted": SID}
    assert [r[0] for r in dashboard.requests] == ["GET", "DELETE", "GET"]
    identity = dashboard.requests[1][2]
    assert re.fullmatch(r"[a-f0-9]{32}", identity["request_id"])
    assert (
        identity["expected_revision"] == "7" and identity["expected_generation"] == "3"
    )
    assert dashboard.effects == 1 and not dashboard.exists


@pytest.mark.asyncio
async def test_concurrent_revision_change_is_not_refreshed_or_retried(dashboard):
    dashboard.race = True
    with pytest.raises(Exception, match="409.*revision_conflict"):
        await hermes.hermes_session(action="delete", session_id=SID)
    assert [r[0] for r in dashboard.requests] == ["GET", "DELETE"]
    assert dashboard.exists and dashboard.effects == 0


@pytest.mark.asyncio
async def test_absent_session_never_sends_delete(dashboard):
    dashboard.exists = False
    with pytest.raises(Exception, match="not found"):
        await hermes.hermes_session(action="delete", session_id=SID)
    assert [r[0] for r in dashboard.requests] == ["GET"]
    assert dashboard.effects == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {
            "session_id": "another",
            "exists": True,
            "runtime_revision": 7,
            "runtime_generation": 3,
        },
        {
            "session_id": SID,
            "exists": True,
            "runtime_revision": True,
            "runtime_generation": 3,
        },
        {
            "session_id": SID,
            "exists": True,
            "runtime_revision": 7,
            "runtime_generation": None,
        },
    ],
)
async def test_unknown_or_malformed_contract_never_falls_back_to_unfenced_delete(
    dashboard, value
):
    if value is None:
        dashboard.snapshot_status = 404
    else:
        dashboard.snapshot_override = value
    with pytest.raises(Exception, match="mutation|contract|snapshot"):
        await hermes.hermes_session(action="delete", session_id=SID)
    assert all(r[0] == "GET" for r in dashboard.requests)
    assert dashboard.effects == 0


@pytest.mark.asyncio
async def test_timeout_after_commit_has_identity_and_never_blind_retries(dashboard):
    dashboard.lose_reply = True
    with pytest.raises(Exception, match="outcome.*unknown") as failure:
        await hermes.hermes_session(action="delete", session_id=SID)
    assert dashboard.effects == 1
    assert [r[0] for r in dashboard.requests] == ["GET", "DELETE"]
    sent = dashboard.requests[1][2]
    assert sent["request_id"] in str(failure.value)
    # An explicit exact identity replay is authorized by the original principal's
    # receipt, even after the row is absent. It must not generate a new identity.
    result = json.loads(
        await hermes.hermes_session(
            action="delete",
            session_id=SID,
            request_id=sent["request_id"],
            expected_revision=7,
            expected_generation=3,
        )
    )
    assert result == {"ok": True, "deleted": SID}
    assert dashboard.effects == 1
    assert [r for r in dashboard.requests if r[0] == "DELETE"][-1][2] == sent


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 409, 503])
async def test_backend_refusal_or_server_ambiguity_is_never_retried(dashboard, status):
    dashboard.delete_status = status
    with pytest.raises(Exception):
        await hermes.hermes_session(action="delete", session_id=SID)
    assert [r[0] for r in dashboard.requests] == ["GET", "DELETE"]
    assert dashboard.effects == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {"request_id": "id"},
        {"expected_revision": 1},
        {"request_id": "id", "expected_revision": 1},
        {"request_id": "id", "expected_revision": True, "expected_generation": 1},
        {"request_id": "id", "expected_revision": -1, "expected_generation": 1},
        {"request_id": "bad?query", "expected_revision": 1, "expected_generation": 1},
    ],
)
async def test_partial_or_invalid_identity_is_rejected_before_network(
    dashboard, identity
):
    with pytest.raises(ValueError, match="identity|request_id|revision"):
        await hermes.hermes_session(action="delete", session_id=SID, **identity)
    assert dashboard.requests == []


@pytest.mark.asyncio
async def test_MCP_schema_and_tool_error_keep_strict_replay_identity(dashboard):
    async with Client(hermes.mcp, mode="legacy") as client:
        tools = (await client.list_tools()).tools
        tool = next(t for t in tools if t.name == "hermes_session")
        assert len(tools) == 11
        assert {
            "request_id",
            "expected_revision",
            "expected_generation",
        } <= tool.input_schema["properties"].keys()
        bad = await client.call_tool(
            "hermes_session",
            {
                "action": "delete",
                "session_id": SID,
                "request_id": "retry",
                "expected_revision": True,
                "expected_generation": 3,
            },
        )
        assert bad.is_error and dashboard.requests == []
        dashboard.lose_reply = True
        failed = await client.call_tool(
            "hermes_session", {"action": "delete", "session_id": SID}
        )
        assert failed.is_error
        text = "\n".join(
            c.text for c in failed.content if getattr(c, "type", "") == "text"
        )
        assert (
            "OUTCOME_UNKNOWN" in text and dashboard.requests[1][2]["request_id"] in text
        )
        assert dashboard.effects == 1


@pytest.mark.asyncio
async def test_same_key_with_different_fences_cannot_reapply(dashboard):
    identity = {
        "request_id": "explicit-owned",
        "expected_revision": 7,
        "expected_generation": 3,
    }
    await hermes.hermes_session(action="delete", session_id=SID, **identity)
    with pytest.raises(Exception, match="admission_conflict"):
        await hermes.hermes_session(
            action="delete", session_id=SID, **{**identity, "expected_generation": 4}
        )
    assert dashboard.effects == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bad_ack", "still_present", "failed_verify"])
async def test_success_ack_is_not_enough_and_verification_never_reissues_delete(
    dashboard, monkeypatch, kind
):
    handler = dashboard.handler

    def changed(request):
        response = handler(request)
        if request.method == "DELETE":
            if kind == "bad_ack":
                return httpx.Response(200, json={"ok": False})
            if kind == "still_present":
                dashboard.exists = True
        if request.method == "GET" and dashboard.effects and kind == "failed_verify":
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return response

    monkeypatch.setattr(dashboard, "handler", changed)
    with pytest.raises(Exception, match="outcome.*unknown"):
        await hermes.hermes_session(action="delete", session_id=SID)
    assert len([r for r in dashboard.requests if r[0] == "DELETE"]) == 1
    assert dashboard.effects == 1


@pytest.mark.asyncio
async def test_invalid_utf8_snapshot_is_safe_and_never_sends_delete(dashboard, monkeypatch):
    original = dashboard.handler

    def changed(request):
        response = original(request)
        if request.method == "GET" and request.url.path.endswith("/mutation-snapshot"):
            return httpx.Response(200, content=b"\xffsnapshot-body-sentinel", request=request)
        return response

    monkeypatch.setattr(dashboard, "handler", changed)
    async with Client(hermes.mcp, mode="legacy") as client:
        failed = await client.call_tool("hermes_session", {"action": "delete", "session_id": SID})
    assert failed.is_error
    assert "BACKEND_UNEXPECTED_RESPONSE: Hermes Dashboard returned non-JSON" in failed.content[0].text
    assert "snapshot-body-sentinel" not in failed.content[0].text
    assert [row[0] for row in dashboard.requests] == ["GET"]
    assert dashboard.effects == 0


@pytest.mark.asyncio
async def test_invalid_utf8_delete_ack_is_unknown_without_retry(dashboard, monkeypatch):
    original = dashboard.handler

    def changed(request):
        response = original(request)
        if request.method == "DELETE":
            return httpx.Response(200, content=b"\xffack-body-sentinel", request=request)
        return response

    monkeypatch.setattr(dashboard, "handler", changed)
    async with Client(hermes.mcp, mode="legacy") as client:
        failed = await client.call_tool("hermes_session", {"action": "delete", "session_id": SID})
    assert failed.is_error
    assert "SESSION_DELETE_OUTCOME_UNKNOWN" in failed.content[0].text
    assert "ack-body-sentinel" not in failed.content[0].text
    assert [row[0] for row in dashboard.requests] == ["GET", "DELETE"]
    assert dashboard.effects == 1


@pytest.mark.asyncio
async def test_deep_409_delete_is_fixed_and_never_retried(dashboard, monkeypatch):
    dashboard.delete_status = 409
    original = dashboard.handler

    def changed(request):
        response = original(request)
        if request.method == "DELETE":
            deep = b"[" * 16384 + b'"synthetic-body-sentinel"' + b"]" * 16384
            return httpx.Response(409, content=deep, request=request)
        return response

    monkeypatch.setattr(dashboard, "handler", changed)
    expected = (
        "BACKEND_HTTP_ERROR status=409: Hermes Dashboard request failed; verify "
        "Connection > Dashboard REST API in the MCP admin page and the operation settings."
    )
    async with Client(hermes.mcp, mode="legacy") as client:
        failed = await client.call_tool("hermes_session", {"action": "delete", "session_id": SID})
    assert failed.is_error
    assert failed.content[0].text == f"Error executing tool hermes_session: {expected}"
    assert "synthetic-body-sentinel" not in failed.content[0].text
    assert [row[0] for row in dashboard.requests] == ["GET", "DELETE"]
    assert dashboard.effects == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_identity", [False, True])
async def test_fenced_delete_401_never_uses_cookie_refresh_retry(
    monkeypatch, explicit_identity
):
    backend = Dashboard()
    backend.delete_status = 401
    logins = []

    async def login(*_):
        logins.append(True)
        return "owned-cookie"

    def client(*_):
        return httpx.AsyncClient(
            base_url="http://fixture.invalid",
            transport=httpx.MockTransport(backend.handler),
        )

    monkeypatch.setattr(
        hermes,
        "_get_connection",
        lambda: {
            "dashboard_url": "http://fixture.invalid",
            "dashboard_username": "owned-user",
            "dashboard_password": "owned-password",
        },
    )
    monkeypatch.setattr(hermes, "_dashboard_login", login)
    monkeypatch.setattr(hermes, "_dashboard_client", client)
    identity = (
        dict(request_id="owned-replay", expected_revision=7, expected_generation=3)
        if explicit_identity
        else {}
    )
    # Exercise the real authenticated client and its send wrapper, not only the
    # session helper's MockTransport seam. A401 must return to this caller once.
    with pytest.raises(Exception, match="401"):
        await hermes.hermes_session(action="delete", session_id=SID, **identity)
    assert [r[0] for r in backend.requests] == ["GET", "DELETE"]
    assert len(logins) == 1
    assert backend.exists and backend.effects == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("second_status", [200, 401])
async def test_nonmutating_401_still_refreshes_cookie_at_most_once(second_status):
    cookies, refreshes = [], []

    def handler(request):
        cookies.append(request.headers.get("Cookie"))
        return httpx.Response(
            401 if len(cookies) == 1 else second_status, json={"ok": True}
        )

    async def refresh():
        refreshes.append(True)
        return "owned-refreshed-cookie"

    async with hermes._relogin_on_401(
        httpx.AsyncClient(
            base_url="http://fixture.invalid", transport=httpx.MockTransport(handler)
        ),
        refresh,
    ) as client:
        response = await client.get("/api/model/info")
    assert response.status_code == second_status
    assert len(cookies) == 2 and len(refreshes) == 1
    assert cookies[-1] == 'hermes_session_at="owned-refreshed-cookie"'
