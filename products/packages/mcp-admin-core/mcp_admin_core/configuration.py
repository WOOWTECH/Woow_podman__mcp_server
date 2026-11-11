"""Snapshot revisions and owned save/apply transitions for management routes.

No middleware, persisted schema changes, backend writes, or proxy stream capture.
HTTP mutations require the revision read by their editor. Internal lifecycle
calls have no HTTP request and remain available without a revision.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

import anyio
from fastapi import HTTPException, Response
from pydantic import BaseModel, Field


class ConfigurationResponse(BaseModel):
    configuration: dict[str, Any] = Field(default_factory=dict)


def launch_revision(store, snapshot: dict) -> str:
    from .launch_policy import get_launch_policy
    from .process import _build_subprocess_env, resolve_launch
    policy = get_launch_policy()
    mcp = snapshot.get("mcp_server", {}) or {}
    command, args, port = resolve_launch(mcp, policy)
    env = _build_subprocess_env(snapshot.get("connection", {}) or {}, mcp, policy, snapshot)
    return store.revision({"command": command, "args": args, "port": port, "env": env}, domain="launch")


def configuration_status(store, snapshot: dict, pm) -> dict:
    from .launch_policy import get_launch_policy
    from .process import resolve_launch
    saved = launch_revision(store, snapshot)
    command, _, _ = resolve_launch(snapshot.get("mcp_server", {}) or {}, get_launch_policy())
    running = bool(getattr(pm, "is_running", False))
    applied = getattr(pm, "_applied_revision", None) if running else None
    attempted = getattr(pm, "_attempted_revision", None)
    error = getattr(pm, "_configuration_error", None) if attempted == saved else None
    if running and applied == saved:
        state = "applied"
    elif error and not getattr(pm, "_verifying_configuration", False):
        state = "failed"
    elif not command:
        state = "unconfigured"
    elif attempted is None and not running:
        state = "unknown"
    else:
        state = "pending"
    return {"server_time_ms": int(time.time() * 1000), "revision": store.revision(snapshot), "saved_revision": saved,
            "applied_revision": applied, "apply_state": state, "last_error_code": error}


def revision_header(response: Response | None, configuration: dict) -> None:
    if response is not None:
        response.headers["ETag"] = '"' + configuration["revision"] + '"'
        response.headers["Cache-Control"] = "no-store"


async def _check_queued_session(request, snapshot):
    """Recheck verified admin claims after waiting; never disclose config on 401.

    Standalone internal calls have no HTTP identity. Product apps use the
    factory's AuthMiddleware and require its verified claims here as well.
    """
    if request is None:
        return
    user = getattr(request.state, "user", None)
    if user is None and not getattr(request.app.state, "admin_auth_required", False):
        return
    generation = user.get("session_generation") if isinstance(user, dict) else None
    current = snapshot.get("admin_session_generation")
    expires = user.get("exp") if isinstance(user, dict) else None
    if (not isinstance(user, dict) or user.get("sub") != "admin"
            or not isinstance(expires, (int, float)) or not expires > time.time()
            or not isinstance(generation, str) or not generation
            or not isinstance(current, str) or not current
            or not secrets.compare_digest(generation.encode(), current.encode())):
        raise HTTPException(401, "Invalid or expired token")
    # Logout can revoke one verified login without changing the generation while
    # its mutation waits for the transition lock. Check before any receipt read,
    # journal write or acceptance; products without this policy keep their flow.
    policy = getattr(request.app.state, "admin_session_policy", None)
    if policy is not None and not await policy.validate(user):
        raise HTTPException(401, "Invalid or expired token")


async def run_configuration_transition(store, pm, if_match, operation, response=None, *, request=None, operation_kind=None, operation_payload=None):
    """Finish an accepted transition even if its HTTP caller is cancelled.

    The owning task keeps the transition lock through persistence, child work,
    and response snapshot. A cancelled request still queued for that lock is
    discarded. Acceptance follows session and revision checks under the lock;
    once accepted, cancellation cannot leave a durable save without child work.
    Disk publication uses ConfigStore's own short lock.
    """
    accepted = False
    async def run():
        nonlocal accepted
        async with store.transition():
            snapshot = await store.load()
            await _check_queued_session(request, snapshot)
            current = configuration_status(store, snapshot, pm)
            journal = None
            operation_id = request.headers.get("Idempotency-Key") if request is not None else None
            if request is not None and operation_kind and operation_id:
                from .operations import OperationJournal
                journal = OperationJournal(store._path)
                data, receipt, fingerprint = await asyncio.to_thread(
                    journal.inspect, operation_id, operation_kind, operation_payload or {})
                if receipt:
                    public = journal.public(operation_id, receipt, replayed=True)
                    if receipt["state"] != "completed":
                        raise HTTPException(409, detail={"code": "operation_outcome_unknown", "operation": public})
                    revision_header(response, current)
                    return {**receipt["result"], "operation": public, "configuration": current}
            if request is not None and if_match is None:
                raise HTTPException(428, detail={"code": "config_revision_required",
                    "message": "請先重新載入設定，再以讀取到的版本儲存。",
                    "configuration": current})
            if if_match is not None and if_match != '"' + current["revision"] + '"':
                raise HTTPException(409, detail={"code": "config_conflict",
                    "message": "設定已由其他操作更新。請重新載入並檢查後再儲存。",
                    "configuration": current})
            if request is not None and operation_kind and not operation_id:
                raise HTTPException(428, detail={"code": "operation_id_required"})
            accepted = True
            if journal:
                await asyncio.to_thread(journal.begin, operation_id, data, operation_kind, fingerprint)
            try:
                result = await operation()
            except Exception as exc:
                if journal:
                    raise HTTPException(409, detail={"code": "operation_outcome_unknown",
                        "operation": {"id": operation_id, "kind": operation_kind, "state": "unknown", "replayed": False}}) from None
                if isinstance(exc, OSError):
                    raise HTTPException(500, detail={"code": "config_save_failed",
                        "message": "設定無法儲存，請先重新載入並核對目前狀態。"}) from None
                raise
            try:
                configuration = configuration_status(store, await store.load(), pm)
            except Exception as exc:
                if journal:
                    raise HTTPException(409, detail={"code": "operation_outcome_unknown",
                        "operation": {"id": operation_id, "kind": operation_kind, "state": "unknown", "replayed": False}}) from None
                if isinstance(exc, OSError):
                    raise HTTPException(500, detail={"code": "config_save_failed",
                        "message": "無法讀回設定，請先核對目前狀態。"}) from None
                raise
            revision_header(response, configuration)
            # Legacy booleans mean spawn succeeded; canonical apply status must
            # not claim protocol readiness until that generation is observed.
            body = result.model_dump() if isinstance(result, BaseModel) else result
            if (hasattr(pm, "_attempted_revision") and body.get("apply_state") == "applied"
                    and configuration["apply_state"] != "applied"):
                updates = {"apply_state": configuration["apply_state"],
                           "status": "partial" if configuration["apply_state"] == "failed" else "ok",
                           "message": "設定已儲存；子程序設定尚未確認套用，後端連線仍需另行驗證。"}
                result = result.model_copy(update=updates) if isinstance(result, BaseModel) else {**result, **updates}
            if journal:
                body = result.model_dump() if isinstance(result, BaseModel) else result
                try:
                    public = await asyncio.to_thread(journal.complete, operation_id, data, body)
                except (OSError, ValueError):
                    raise HTTPException(409, detail={"code": "operation_outcome_unknown",
                        "operation": {"id": operation_id, "kind": operation_kind, "state": "unknown", "replayed": False}}) from None
                return {**body, "operation": public, "configuration": configuration}
            if isinstance(result, BaseModel):
                return result.model_copy(update={"configuration": configuration})
            return {**result, "configuration": configuration}

    task = asyncio.create_task(run())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not accepted:
            task.cancel()
        # AnyIO cancellation is level-triggered. asyncio.shield alone still
        # re-enters a cancelled scope at every await and can spin indefinitely.
        # Also tolerate repeated direct Task.cancel() while joining ownership.
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()  # retrieve failure; the caller remains cancelled
        raise
