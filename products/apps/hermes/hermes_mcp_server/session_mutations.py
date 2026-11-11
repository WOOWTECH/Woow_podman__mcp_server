"""The Agent owns session fencing and receipts; the adapter never retries a delete."""

from __future__ import annotations

import json
import re
import uuid

import httpx

from .errors import (
    BACKEND_UNEXPECTED_RESPONSE,
    NOT_FOUND,
    HermesBackendError,
    coded_error,
    dashboard_json,
)
from .guards import GuardError, path_segment


def mutation_identity(
    request_id, expected_revision, expected_generation
) -> dict | None:
    """A new operation has no identity; an explicit replay must supply all fences."""
    values = (request_id, expected_revision, expected_generation)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise GuardError(
            "session mutation identity requires request_id, expected_revision and expected_generation together"
        )
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", request_id
    ):
        raise GuardError("invalid request_id: use 1–128 letters, digits, '_' or '-'")
    if any(
        type(value) is not int or value < 0
        for value in (expected_revision, expected_generation)
    ):
        raise GuardError(
            "session mutation identity revision and generation must be nonnegative integers"
        )
    return dict(
        request_id=request_id,
        expected_revision=expected_revision,
        expected_generation=expected_generation,
    )


async def _snapshot(client: httpx.AsyncClient, path: str, session_id: str) -> dict:
    response = await client.get(path + "/mutation-snapshot")
    if response.status_code in (404, 405):
        raise coded_error(
            BACKEND_UNEXPECTED_RESPONSE,
            "Hermes Dashboard session mutation-snapshot contract is unavailable. "
            "No delete was sent; a backend with the fenced session mutation API is required.",
        )
    response.raise_for_status()
    value = dashboard_json(response)
    valid = (
        isinstance(value, dict)
        and value.get("session_id") == session_id
        and type(value.get("exists")) is bool
    )
    if valid:
        revision, generation = (
            value.get("runtime_revision"),
            value.get("runtime_generation"),
        )
        valid = type(revision) is int and revision >= 0
        valid = valid and (
            type(generation) is int and generation >= 0
            if value["exists"]
            else revision == 0 and generation is None
        )
    if not valid:
        raise coded_error(
            BACKEND_UNEXPECTED_RESPONSE,
            "Hermes returned an invalid session mutation snapshot; no unfenced delete is allowed.",
        )
    return value


def _unknown(identity: dict, stage: str) -> HermesBackendError:
    return HermesBackendError(
        f"SESSION_DELETE_OUTCOME_UNKNOWN: Hermes session delete outcome is unknown ({stage}). "
        "No automatic retry was made. Inspect the session before deciding to retry; "
        "an explicit retry must keep this exact identity: "
        + json.dumps(identity, sort_keys=True)
    )


async def delete_session(
    client: httpx.AsyncClient, session_id: str, identity: dict | None = None
) -> None:
    path = "/api/sessions/" + path_segment(session_id, "session_id")
    snapshot = await _snapshot(client, path, session_id)
    if identity is None:
        if not snapshot["exists"]:
            raise coded_error(
                NOT_FOUND, f"Hermes session {session_id!r} not found; nothing was deleted"
            )
        identity = dict(
            request_id=uuid.uuid4().hex,
            expected_revision=snapshot["runtime_revision"],
            expected_generation=snapshot["runtime_generation"],
        )
    # Explicit identity replays keep the original fences, even if the row disappeared
    # or changed. Only the Agent's authenticated receipt/CAS can decide their outcome.
    try:
        response = await client.delete(
            path, params=identity, extensions={"hermes_no_auth_retry": True}
        )
    except httpx.TransportError:
        raise _unknown(identity, "reply unavailable") from None
    if response.status_code >= 500:
        raise _unknown(identity, f"HTTP {response.status_code}")
    response.raise_for_status()  # A 409 is final; do not re-read fences and resend.
    try:
        result = dashboard_json(response)
    except ValueError:
        raise _unknown(identity, "invalid acknowledgement") from None
    if isinstance(result, dict) and result.get("already_absent") is True:
        raise coded_error(
            NOT_FOUND,
            f"Hermes session {session_id!r} not found; no committed delete receipt was returned",
        )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise _unknown(identity, "invalid acknowledgement")
    if "deleted_ids" in result and (
        not isinstance(result["deleted_ids"], list)
        or session_id not in result["deleted_ids"]
    ):
        raise _unknown(identity, "acknowledgement did not name the requested session")
    try:
        after = await _snapshot(client, path, session_id)
    except (httpx.HTTPError, ValueError, HermesBackendError):
        raise _unknown(identity, "absence verification unavailable") from None
    if after["exists"]:
        raise _unknown(identity, "session remains present after acknowledgement")
