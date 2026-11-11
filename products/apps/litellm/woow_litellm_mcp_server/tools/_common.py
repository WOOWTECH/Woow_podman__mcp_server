"""Shared helpers for tool modules: MCP annotations and paging.

The ``[DESTRUCTIVE]`` docstring prefix convention marks mutating tools so that
both humans and the admin GUI can spot them; those same tools carry
``destructive`` annotations.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

try:
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover - fallback if mcp types unavailable

    class ToolAnnotations:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

from ..errors import ToolError


def read_only(title: str) -> ToolAnnotations:
    """Annotation for a side-effect-free read tool."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


def writing(title: str) -> ToolAnnotations:
    """Annotation for a create/update tool that is not destructive."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )


def destructive(title: str) -> ToolAnnotations:
    """Annotation for a delete/block tool with irreversible effects."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )


def path_segment(value: str, field: str) -> str:
    """One URL path segment, percent-encoded including any '/', '?' or '#'.

    '', '.' and '..' are refused: quote() leaves dots alone and httpx drops '.' and '..' segments while it builds the
    URL, so a key '../../global/spend/reset?' spliced as-is made regenerate POST /global/spend/reset with the master key.
    """
    if value in ("", ".", ".."):
        raise ToolError(f"{field} must not be empty, '.' or '..'.")
    return quote(value, safe="")


def page_of(
    items: list[Any], page: int = 1, size: int = 50
) -> dict[str, Any]:
    """Slice a list into a page envelope: {items, page, size, total}."""
    page = max(1, int(page))
    size = max(1, int(size))
    start = (page - 1) * size
    end = start + size
    return {
        "items": items[start:end],
        "page": page,
        "size": size,
        "total": len(items),
    }


def prune_none(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None so we never send nulls to LiteLLM."""
    return {k: v for k, v in data.items() if v is not None}
