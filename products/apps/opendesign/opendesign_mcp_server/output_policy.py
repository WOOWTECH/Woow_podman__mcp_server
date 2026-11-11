"""Bound OpenDesign metadata returned to MCP clients.

The two projections follow the HA OpenDesign 0.2.14 ``od_rules`` contract.
Keep this overlay outside the byte-faithful vendored tool module.
"""

from __future__ import annotations

import functools
import inspect
import re
from typing import Any

import httpx

CONNECTOR_LIMIT = 300
CONNECTOR_TOOL_LIMIT = 50
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
RUN_FIELDS = ("id", "projectId", "status")
RUN_LIMIT = 100
RUN_TEXT_LIMIT = 256
PROJECTORS = {"list_connectors", "list_runs"}


def _ident(value: Any) -> str | None:
    return value if isinstance(value, str) and IDENT.fullmatch(value) else None


def _label(value: Any, limit: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    value = CONTROL.sub("", value).strip()
    return value[:limit] or None


def project_connectors(body: Any) -> dict:
    """Return bounded identity, status and tool names; omit auth and paths."""
    items = body.get("connectors") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ValueError("unexpected connector list")  # noqa: TRY004 - HA contract
    connectors = []
    for item in items[:CONNECTOR_LIMIT]:
        if not isinstance(item, dict) or _ident(item.get("id")) is None:
            continue
        tools = item.get("tools")
        names = ([tool.get("name") for tool in tools if isinstance(tool, dict)] if isinstance(tools, list)
                 else item.get("allowedToolNames") if isinstance(item.get("allowedToolNames"), list) else [])
        names = [name for name in names if _ident(name) is not None]
        auth = item.get("auth")
        configured = auth.get("configured") if isinstance(auth, dict) else None
        count = item.get("toolCount")
        entry = {
            "id": item["id"],
            "name": _label(item.get("name")),
            "provider": _ident(item.get("provider")),
            "category": _label(item.get("category"), 64),
            "status": _ident(item.get("status")),
            "configured": configured if type(configured) is bool else None,
            "toolCount": count if type(count) is int and count >= 0 else len(names),
            "tools": names[:CONNECTOR_TOOL_LIMIT],
        }
        if len(names) > CONNECTOR_TOOL_LIMIT:
            entry["toolsTruncated"] = True
        connectors.append(entry)
    return {"total": len(items), "connectors": connectors, "truncated": len(items) > CONNECTOR_LIMIT}


def run_rows(body: Any) -> dict:
    """Return at most 100 runs with only bounded id, projectId and status."""
    values = body.get("runs") if isinstance(body, dict) else body
    if not isinstance(values, list):
        raise ValueError("unexpected metadata envelope")  # noqa: TRY004 - HA contract
    return {"runs": [_run(row) for row in values[:RUN_LIMIT]], "truncated": len(values) > RUN_LIMIT}


def _run(body: Any) -> dict:
    if not isinstance(body, dict):
        raise ValueError("unexpected metadata record")  # noqa: TRY004 - HA contract
    result = {}
    for key in RUN_FIELDS:
        value = body.get(key)
        if value is None or type(value) in (bool, int, float):
            result[key] = value
        elif type(value) is str:
            result[key] = value[:RUN_TEXT_LIMIT]
    return result


def _public_backend_error(error: Exception) -> str:
    """Classify without including the daemon URL, path or exception text."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"BACKEND_HTTP_ERROR status={error.response.status_code}"
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "BACKEND_TIMEOUT"
    if isinstance(error, (httpx.DecodingError, UnicodeError, ValueError)):
        return "BACKEND_INVALID_RESPONSE"
    return "BACKEND_UNAVAILABLE"


def install_output_guards(server: Any) -> int:
    """Wrap the two known tools, retaining their SDK parameter and schema data."""
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    if not PROJECTORS <= set(tools):
        raise RuntimeError("OpenDesign metadata tools changed; review output policy")
    count = 0
    for name in PROJECTORS:
        tool = tools[name]
        if inspect.signature(tool.fn).parameters:
            raise RuntimeError("OpenDesign metadata arguments changed; review output policy")
        if getattr(tool.fn, "__od_output_guarded__", False):
            continue
        original = tool.fn
        projector = project_connectors if name == "list_connectors" else run_rows

        @functools.wraps(original)
        def guarded(*args: Any, __fn=original, __projector=projector, **kwargs: Any) -> dict:
            try:
                return __projector(__fn(*args, **kwargs))
            except Exception as error:  # noqa: BLE001 - public MCP error boundary
                raise ValueError(_public_backend_error(error)) from None

        guarded.__od_output_guarded__ = True
        tool.fn = guarded
        count += 1
    return count
