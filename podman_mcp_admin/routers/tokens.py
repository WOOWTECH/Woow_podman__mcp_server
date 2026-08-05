"""MCP proxy token: generate, preview, rotate.

Generation and rotation are deliberately separate — previewing a token must
not invalidate the one AI clients are currently using.

The history entries written here are the shared contract with
``mcp_admin_core.routers.settings`` (the other rotate endpoint) and with the
React ``TokenManager`` page: exactly ``{"masked": …, "rotated_at": …}``. The two
writers used to disagree (one wrote ``{"token_masked": …}``, the other a bare
string), so the page rendered "[object Object]" — or crashed React outright by
being handed a raw object as a child — depending on which endpoint had last
rotated.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from ..store import mask_secret

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

# The page shows a short audit trail; ten rotations is enough to answer "when
# did this change?" without growing config.json without bound.
_KEEP_HISTORY = 10


def _history_entry(token: str) -> dict[str, str]:
    """One audit row for a token that has just been retired."""
    return {
        "masked": mask_secret(token),
        "rotated_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalise_history(history: Any) -> list[dict[str, str]]:
    """Coerce older shapes into the contract the SPA renders.

    Config files written by earlier builds hold bare strings or
    ``{"token_masked": …}``; passing either straight through meant the page
    printed "[object Object]" or masked nothing at all.
    """
    entries: list[dict[str, str]] = []
    for item in list(history or []):
        if isinstance(item, dict):
            masked = item.get("masked") or item.get("token_masked") or ""
            entries.append(
                {
                    "masked": str(masked),
                    "rotated_at": str(item.get("rotated_at") or item.get("at") or ""),
                }
            )
        else:
            # A legacy bare string is already masked; keep it, timestamp unknown.
            entries.append({"masked": str(item), "rotated_at": ""})
    return entries[:_KEEP_HISTORY]


@router.get("")
async def get_token() -> dict[str, Any]:
    from mcp_admin_core.config import get_config_store

    store = get_config_store()
    token = await store.get("mcp_auth_token", "")
    return {
        # Never more than four leading characters: the token is the only thing
        # standing between the public proxy URL and the MCP server.
        "masked": mask_secret(token),
        "configured": bool(token),
        "history": _normalise_history(await store.get("token_history", [])),
    }


@router.post("/generate")
async def generate_token() -> dict[str, Any]:
    """Preview a new token. Nothing is persisted until /rotate."""
    return {"token": secrets.token_urlsafe(32)}


@router.post("/rotate")
async def rotate_token() -> dict[str, Any]:
    """Generate, persist and activate a new token in one step."""
    from mcp_admin_core.config import get_config_store
    from mcp_admin_core.process import get_process_manager

    store = get_config_store()
    previous = await store.get("mcp_auth_token", "")
    token = secrets.token_urlsafe(32)

    history = _normalise_history(await store.get("token_history", []))
    if previous:
        # Newest first — the page lists the most recent rotation at the top.
        history = ([_history_entry(previous)] + history)[:_KEEP_HISTORY]

    await store.put("mcp_auth_token", token)
    await store.put("token_history", history)

    manager = get_process_manager()
    status = "ok"
    if manager.is_running and not await manager.restart():
        status = "partial"

    # The plaintext token is returned exactly once, on the response to the
    # rotation that minted it; every later read is masked.
    return {
        "token": token,
        "masked": mask_secret(token),
        "status": status,
        "history": history,
    }
