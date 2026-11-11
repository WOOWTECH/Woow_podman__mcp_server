"""Shared helpers for tool modules."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations


def read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


def destructive(title: str, idempotent: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=idempotent,
        openWorldHint=True,
    )


def writing(title: str) -> ToolAnnotations:
    """Changes state but destroys nothing (e.g. publishing a message)."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )


def path_segment(value: str, field: str, safe: str = "") -> str:
    """One URL path segment, percent-encoded including any '/' (``safe`` keeps a few characters as they are).

    '', '.' and '..' are refused: quote() leaves dots alone and httpx drops '.' and '..' segments while it builds the
    URL, so unbanning '..' became DELETE /banned (every ban cleared) and deleting user '..' deleted its authenticator.
    """
    if value in ("", ".", ".."):
        raise ToolError(f"{field} must not be empty, '.' or '..'.")
    return quote(value, safe=safe)


def page_of(body: Any) -> tuple[list[dict], dict]:
    """Split an EMQX paged response into (rows, meta)."""
    if isinstance(body, dict):
        rows = body.get("data", [])
        return (rows if isinstance(rows, list) else []), body.get("meta", {}) or {}
    if isinstance(body, list):
        return body, {}
    return [], {}
