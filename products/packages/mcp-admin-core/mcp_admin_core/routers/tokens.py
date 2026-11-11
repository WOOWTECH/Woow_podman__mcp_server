"""Shared MCP credential lifecycle: masked state, issue, rotate and revoke.

Changes are persisted atomically and invalidate in-flight proxy leases. The child
process is not restarted; dispatched backend work is not promised cancelled.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from mcp_admin_core.config import get_config_store
from mcp_admin_core.util.mask import mask_secret
from mcp_admin_core.configuration import (ConfigurationResponse, configuration_status,
    revision_header, run_configuration_transition)
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.profiles import current_profile_id, get_profile_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_HISTORY = 5
DEFAULT_TOKEN_LENGTH = 64  # hex chars


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class TokenInfo(ConfigurationResponse):
    """Current token state."""

    token_masked: str
    token_set: bool
    state: str = "unconfigured"
    shared: bool = True
    last_rotated: str | None = None
    history: list[dict[str, str]] = []


class TokenGenerateResponse(BaseModel):
    """Generated token (shown once, not persisted until rotate/set)."""

    token: str
    length: int


class TokenSetRequest(BaseModel):
    """Payload for setting a specific token value."""

    token: str = Field(..., min_length=16, max_length=256, pattern=r"^[A-Za-z0-9_-]+$", description="URL-safe shared token value to set")


class TokenRotateRequest(BaseModel):
    """Payload for rotating the token."""

    length: int = Field(64, ge=32, le=256, description="Length of the generated hex token")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def new_token(length: int = DEFAULT_TOKEN_LENGTH) -> str:
    """產生 MCP token（*length* 個 hex 字元）：管理頁輪替、預覽與第一次啟動自動發的 token 都用這一個。"""
    return secrets.token_hex((length + 1) // 2)[:length]


def _apply_result(*, revoked: bool = False) -> dict[str, str]:
    return {"status": "ok", "token_state": "revoked" if revoked else "applied",
            "child_state": "unchanged", "message": (
                "Shared token revoked. All clients are blocked and existing proxy streams are closed. "
                if revoked else "Shared token applied. Previous credentials and proxy streams are invalidated. "
            ) + "Already dispatched backend work may continue; this does not undo its effects."}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("", response_model=TokenInfo)
async def get_token_info(response: Response = None) -> TokenInfo:
    """Return current proxy token (masked) and rotation history."""
    try:
        store = get_config_store()
        cfg = await store.load()
        configuration = configuration_status(store, cfg, get_process_manager())
        revision_header(response, configuration)
        revoked = cfg.get("mcp_auth_revoked", False)
        current_token = "" if revoked else cfg.get("mcp_auth_token", "")
        history = cfg.get("token_history", [])[:MAX_HISTORY]

        return TokenInfo(
            configuration=configuration,
            token_masked=mask_secret(current_token, prefix=4, suffix=4, min_len=8, max_stars=20) if current_token else "",
            token_set=bool(current_token),
            state="revoked" if revoked else "active" if current_token else "unconfigured",
            last_rotated=history[0].get("rotated_at") if history else None,
            history=history,
        )
    except Exception as exc:
        logger.error("Failed to read token info: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read token info: {exc}",
        ) from exc


@router.post("/generate", response_model=TokenGenerateResponse)
async def generate_token(length: int = 64) -> TokenGenerateResponse:
    """Generate a random hex token (preview only, not applied)."""
    if length < 32 or length > 256:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token length must be between 32 and 256",
        )
    token = new_token(length)
    return TokenGenerateResponse(token=token, length=len(token))


@router.post("/rotate", response_model=dict[str, Any])
async def rotate_token(payload: TokenRotateRequest | None = None, response: Response = None,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        request: Request = None) -> dict[str, Any]:
    """Issue one shared credential without restarting the MCP child."""
    length = payload.length if payload else DEFAULT_TOKEN_LENGTH
    store = get_config_store()
    async def apply_update():
        token = new_token(length)
        await store.change_mcp_token(token, action="rotate")
        return {**_apply_result(), "token": token}
    return await run_configuration_transition(store, get_process_manager(), if_match,
        apply_update, response, request=request, operation_kind="token.rotate", operation_payload={"length": length})


@router.put("", response_model=dict[str, Any])
async def set_token(payload: TokenSetRequest, response: Response = None,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        request: Request = None) -> dict[str, Any]:
    store = get_config_store()
    registry = get_profile_registry()
    # Reject missing profile files before admitting a durable operation. The
    # claim below repeats this check under the assignment lock for races.
    await registry.resolve_token(payload.token, strict=True)
    async def apply_update():
        await registry.assign_token(current_profile_id(), payload.token)
        return _apply_result()
    return await run_configuration_transition(store, get_process_manager(), if_match,
        apply_update, response, request=request, operation_kind="token.set", operation_payload={"token": payload.token})


@router.post("/revoke", response_model=dict[str, Any])
async def revoke_token(response: Response = None,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        request: Request = None) -> dict[str, Any]:
    """Disable the shared credential until an administrator explicitly reissues it."""
    store = get_config_store()
    async def apply_update():
        await store.change_mcp_token(None, action="revoke")
        return _apply_result(revoked=True)
    return await run_configuration_transition(store, get_process_manager(), if_match,
        apply_update, response, request=request, operation_kind="token.revoke")
