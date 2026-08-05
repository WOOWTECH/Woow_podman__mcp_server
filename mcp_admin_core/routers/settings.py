"""Settings router — full config.json management via Web GUI.

Provides read/patch access to the config sections: connection, mcp_server,
proxy, tools, admin_password, mcp_auth_token.

Two invariants run through this module and are load-bearing:

*   **Every read is masked.**  ``GET /api/settings`` and
    ``GET /api/settings/{section}`` both run through :func:`mask_secret`, so a
    stolen admin JWT (or a HAR capture, or an XSS in the SPA) cannot lift the
    Podman TLS material, the admin password or the connector's ``mcp_auth_token``
    verbatim.  The per-section route used to bypass the masking entirely.
*   **Every write is a MERGE.**  ``PUT /api/settings/{section}`` patches the
    stored section; omitted keys keep their stored values and a secret echoed
    back in its *masked* form never overwrites the real one.  The previous
    whole-section replace meant a partial PUT to ``connection`` deleted
    ``podman_uri`` and then restarted the child without it, i.e. the
    GUI could take the live connector down.

Endpoints:
    GET  /api/settings           - Full config (all secrets masked)
    GET  /api/settings/{section} - Get one section (all secrets masked)
    PUT  /api/settings/{section} - Merge into one section
    POST /api/settings/mcp_auth_token/rotate - Rotate proxy path token
    POST /api/settings/mcp/restart - Restart MCP server process
    GET  /api/settings/mcp/status  - MCP server process status
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import get_config_store, mask_secret
from ..process import get_process_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class McpServerConfig(BaseModel):
    """MCP server process configuration (full shape, as stored)."""

    command: str = Field("", description="Executable command (e.g. 'python')")
    args: list[str] = Field(default_factory=list, description="Command-line arguments")
    port: int = Field(8000, ge=1, le=65535, description="MCP server listen port")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")


class ProxyConfig(BaseModel):
    """MCP proxy configuration (full shape, as stored)."""

    timeout: int = Field(86400, ge=1, le=604800, description="Proxy timeout in seconds")
    bearer_token: str | None = Field(None, description="Optional Bearer token for upstream")


class ConnectionConfig(BaseModel):
    """Upstream service connection settings (Podman host).

    For Podman the connection carries ``podman_uri`` (a unix:// or tcp://
    socket address), ``podman_api_version``, and the optional TLS triple
    ``podman_tls_ca`` / ``podman_tls_cert`` / ``podman_tls_key`` — which are
    FILE PATHS, not secrets; see ``_NON_SECRET_KEYS``.  Kept generic so the
    routers use whatever keys are present.
    """

    # Generic — routers use whatever keys are present
    model_config = {"extra": "allow"}


class ToolsConfig(BaseModel):
    """Tool enable/disable settings — the shape the tool gate actually reads."""

    readonly: bool = False
    disabled_categories: list[str] = Field(default_factory=list)
    disabled_tools: list[str] = Field(default_factory=list)
    disabled_operations: dict[str, list[str]] = Field(default_factory=dict)


# -- Patch models: every field optional, unknown keys rejected ---------------
#
# These bind the *request body* of PUT /api/settings/{section}.  They exist so
# a typo'd key ("prot": 3000) is a 422 instead of a silently persisted junk key
# the GUI can never clear, and so an out-of-range port cannot be written at all.
# All fields default to ``None`` = "not supplied" = keep the stored value.


class _McpServerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    args: list[str] | None = None
    port: int | None = Field(None, ge=1, le=65535)
    env: dict[str, str] | None = None


class _ProxyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout: int | None = Field(None, ge=1, le=604800)
    bearer_token: str | None = None


class FullConfig(BaseModel):
    """Complete config for GET /api/settings — no secret leaves in the clear.

    ``mcp_auth_token`` is deliberately absent: it is the single string that
    authenticates the public ``/private_<token>/mcp/`` connector URL, and this
    response is fetched by every page that mounts.  Callers that need to show
    it use ``GET /api/tokens`` (masked) or the one-shot value returned by a
    rotation.
    """

    admin_password_masked: str = ""
    admin_password_configured: bool = False
    mcp_auth_token_masked: str = ""
    mcp_auth_token_configured: bool = False
    connection: dict[str, Any] = {}
    mcp_server: dict[str, Any] = {}
    proxy: dict[str, Any] = {}
    tools: dict[str, Any] = {}


class McpProcessStatus(BaseModel):
    """MCP server process status.

    ``running`` is process liveness; ``ready`` is "the child is accepting
    connections on its port".  They differ for the 10-14s the FastMCP child
    needs to bind after a restart — reporting only ``running`` told the
    operator "healthy" while the connector was still answering 502.
    """

    running: bool
    ready: bool = False
    state: str = "stopped"
    pid: int | None = None
    restart_count: int = 0
    command: str = ""
    port: int = 8000
    exit_code: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Sections this router will read.  Anything else is a 404 rather than a window
# onto arbitrary top-level config keys.
_READABLE_SECTIONS = frozenset(
    {"connection", "mcp_server", "proxy", "tools", "admin_password", "mcp_auth_token"}
)

# Sections this router will write.  ``tools`` is deliberately NOT here: writing
# it from this route persisted a value nothing applies to the running child
# (see put_section).
_WRITABLE_SECTIONS = frozenset({"connection", "mcp_server", "proxy", "admin_password"})

# Bare scalar secrets.  They are never returned under a "value" key, so a
# client cannot round-trip the mask back into the store by accident.
_SECRET_SCALAR_SECTIONS = frozenset({"admin_password", "mcp_auth_token"})

_SECRET_KEY_HINTS = ("key", "token", "password", "secret")

# Substring matching is a heuristic, and Podman breaks it: ``podman_tls_key``
# and ``podman_tls_cert`` are FILE PATHS on the host, not credentials.  Masked
# as secrets they render as ``/etc…`` in the GUI, and — worse — the
# mask-preserving merge in ``_merge_preserving_secrets`` treats an empty string
# as "unchanged", so the operator can never CLEAR a TLS path once it is set.
# Anything listed here is exempt from both masking and mask-preservation.
_NON_SECRET_KEYS = frozenset({
    "podman_tls_ca",
    "podman_tls_cert",
    "podman_tls_key",
})

# Connection keys are upper-cased into the child's environment
# (mcp_admin_core.process), so they must be valid environment variable names.
_ENV_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Shortest password this endpoint will accept.  The old floor was 4 characters,
# which is not a password.
_MIN_PASSWORD_LEN = 8

_KEEP_HISTORY = 10

# Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY and warns on the old name;
# the numeric code is the stable part.
_HTTP_422 = 422


def _mask(value: str) -> str:
    """Backwards-compatible alias for the canonical masker (see C4)."""
    return mask_secret(value)


def _is_secret_key(name: str) -> bool:
    lowered = str(name).lower()
    if lowered in _NON_SECRET_KEYS:
        return False
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _mask_mapping(data: Any) -> Any:
    """Return *data* with every secret-looking leaf masked.

    Recursive because ``mcp_server.env`` is a dict of environment variables and
    is exactly where a master key or bearer token ends up.
    """
    if isinstance(data, dict):
        masked: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                masked[key] = _mask_mapping(value)
            elif _is_secret_key(key):
                masked[key] = mask_secret(str(value)) if value else ""
            else:
                masked[key] = value
        return masked
    if isinstance(data, list):
        return [_mask_mapping(item) for item in data]
    return data


def _merge_preserving_secrets(stored: dict[str, Any], updates: dict[str, Any], *, section: str) -> dict[str, Any]:
    """Merge *updates* into *stored*, refusing to overwrite a secret with its mask.

    Two failure modes this prevents:

    * A partial PUT (the GUI saving one field) dropping every key it did not
      send — that is how ``podman_uri`` used to vanish.
    * The GUI's own save round-trip: it renders the masked value it just read,
      so an untouched field posts ``"sk-b…"`` back.  Writing that would destroy
      the credential just as thoroughly as deleting it.
    """
    merged = dict(stored)
    for key, value in updates.items():
        current = stored.get(key)
        if _is_secret_key(key) and isinstance(value, str):
            if current and value == mask_secret(str(current)):
                continue  # the mask was echoed back — keep what is stored
            if not value and section == "connection" and current:
                # Parity with PUT /api/config/connection: an untouched secret
                # field arrives empty and must not wipe the live credential.
                continue
        if isinstance(value, dict) and isinstance(current, dict):
            # A nested object (``mcp_server.env``) is sent whole by the editor
            # that owns it, so it REPLACES — otherwise a removed variable could
            # never be deleted.  Masked secrets inside it are still restored.
            merged[key] = _restore_masked_leaves(current, value)
        else:
            merged[key] = value
    return merged


def _restore_masked_leaves(stored: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """*incoming* wins key-for-key, except where it merely echoes a mask."""
    result: dict[str, Any] = {}
    for key, value in incoming.items():
        current = stored.get(key)
        if (
            _is_secret_key(key)
            and isinstance(value, str)
            and current
            and value == mask_secret(str(current))
        ):
            result[key] = current
            continue
        if isinstance(value, dict) and isinstance(current, dict):
            result[key] = _restore_masked_leaves(current, value)
        else:
            result[key] = value
    return result


def _validate_connection(body: dict[str, Any]) -> dict[str, Any]:
    """Validate a generic connection patch.

    ``connection`` stays schema-free (each product ships its own keys), but the
    keys become environment variable names in the MCP child, so anything that
    is not a valid identifier — or a value that is not a scalar — is rejected
    here rather than breaking the subprocess launch later.
    """
    cleaned: dict[str, Any] = {}
    for key, value in body.items():
        if not _ENV_SAFE_KEY.match(str(key)):
            raise HTTPException(
                _HTTP_422,
                f"Invalid connection key: {key!r} (must be a valid environment variable name)",
            )
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise HTTPException(
                _HTTP_422,
                f"Connection value for {key!r} must be a scalar",
            )
        cleaned[key] = value
    return cleaned


def _validate_patch(model: type[BaseModel], body: dict[str, Any]) -> dict[str, Any]:
    """Bind *body* to *model*, returning only the keys that were supplied."""
    try:
        parsed = model(**body)
    except ValidationError as exc:
        raise HTTPException(_HTTP_422, exc.errors(include_url=False)) from exc
    return parsed.model_dump(exclude_unset=True)


def _history_entry(token: str) -> dict[str, str]:
    """One audit row for a token that has just been retired.

    Exactly the shape ``podman_mcp_admin.routers.tokens`` writes and the React
    TokenManager renders: ``{"masked": …, "rotated_at": …}``.  The two rotate
    endpoints used to disagree, so the page printed "[object Object]".
    """
    return {
        "masked": mask_secret(token),
        "rotated_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalise_history(history: Any) -> list[dict[str, str]]:
    """Coerce older history shapes (bare strings, ``token_masked``) into the contract."""
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
            entries.append({"masked": str(item), "rotated_at": ""})
    return entries[:_KEEP_HISTORY]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=FullConfig)
async def get_settings() -> FullConfig:
    """Return full config with every sensitive value masked."""
    store = get_config_store()
    cfg = await store.load()

    password = str(cfg.get("admin_password", "") or "")
    token = str(cfg.get("mcp_auth_token", "") or "")

    return FullConfig(
        admin_password_masked=mask_secret(password) if password else "(not set)",
        admin_password_configured=bool(password),
        mcp_auth_token_masked=mask_secret(token),
        mcp_auth_token_configured=bool(token),
        connection=_mask_mapping(cfg.get("connection", {}) or {}),
        mcp_server=_mask_mapping(cfg.get("mcp_server", {}) or {}),
        proxy=_mask_mapping(cfg.get("proxy", {}) or {}),
        # Masked like every other section: GET /api/settings/tools already runs
        # through _mask_mapping, and the two read paths must not disagree about
        # what a secret-looking key is worth showing (C4).
        tools=_mask_mapping(cfg.get("tools", {}) or {}),
    )


@router.get("/{section}")
async def get_section(section: str) -> dict[str, Any]:
    """Get a single config section, masked exactly like GET /api/settings."""
    if section not in _READABLE_SECTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown section: {section}")

    store = get_config_store()
    value = await store.get(section)

    if section in _SECRET_SCALAR_SECTIONS:
        # No "value" key at all: the GUI only ever needs "is it set, and which
        # one is it", and omitting the key stops a client from PUTting the mask
        # straight back over the real secret.
        text = str(value or "")
        return {"configured": bool(text), "masked": mask_secret(text)}

    if value is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown section: {section}")
    if isinstance(value, dict):
        return _mask_mapping(value)
    return {"value": value}


async def _put_admin_password(body: dict[str, Any]) -> dict[str, str]:
    """Change the admin password — proof of the current one required.

    Without the current-password check, anything holding an admin JWT for the
    12h it stays valid (including an XSS that reads it out of localStorage)
    could lock the real operator out of the console.
    """
    from ..auth.middleware import passwords_match, revoke_all_sessions

    # Accept the field spellings the SPA has used across revisions rather than
    # failing a save on a name mismatch.
    new_pass = str(body.get("value") or body.get("new_password") or body.get("password") or "")
    current = str(body.get("current") or body.get("current_password") or body.get("old_password") or "")

    if len(new_pass) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Password must be at least {_MIN_PASSWORD_LEN} characters",
        )

    store = get_config_store()
    stored = str(await store.get("admin_password", "") or "")
    if stored and not passwords_match(stored, current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")

    await store.put("admin_password", new_pass)
    # A password change must not leave older JWTs (or the HttpOnly cookie on
    # another machine) authorised.
    revoke_all_sessions()
    logger.warning("Admin password changed; all existing sessions revoked")
    return {"status": "ok", "message": "Admin password updated"}


@router.put("/{section}")
async def put_section(section: str, body: dict[str, Any]) -> dict[str, str]:
    """Merge into a config section. Restarts MCP server if connection or mcp_server changed."""
    if section == "tools":
        # Writing here persisted switches that never reached the running child:
        # the gate is fed from mcp_server.env by PUT /api/tools, which this
        # route cannot call (that router lives in the product layer).
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Use PUT /api/tools — tool switches written here never reach the running MCP server",
        )
    if section not in _WRITABLE_SECTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown section: {section}")

    store = get_config_store()

    # Special handling for admin_password (a scalar, not a section)
    if section == "admin_password":
        return await _put_admin_password(body)

    if section == "connection":
        updates = _validate_connection(body)
    elif section == "mcp_server":
        updates = _validate_patch(_McpServerPatch, body)
    else:  # proxy
        updates = _validate_patch(_ProxyPatch, body)

    stored = await store.get(section, {}) or {}
    if not isinstance(stored, dict):
        stored = {}
    merged = _merge_preserving_secrets(stored, updates, section=section)
    await store.put(section, merged)
    logger.info("Updated config section: %s (keys=%s)", section, sorted(updates))

    # Auto-restart MCP server if relevant config changed
    if section in ("connection", "mcp_server"):
        pm = get_process_manager()
        if pm.is_running:
            restarted = await pm.restart()
            if not restarted:
                # Saved, but the child is not back yet — say so instead of
                # reporting a healthy restart the connector cannot honour.
                return {
                    "status": "partial",
                    "message": f"Section '{section}' updated, MCP server not ready yet",
                }
            return {"status": "ok", "message": f"Section '{section}' updated, MCP server restarted"}

    return {"status": "ok", "message": f"Section '{section}' updated"}


@router.post("/mcp_auth_token/rotate")
async def rotate_mcp_token() -> dict[str, Any]:
    """Generate a new random MCP auth token (the proxy path token)."""
    store = get_config_store()
    cfg = await store.load()

    old_token = str(cfg.get("mcp_auth_token", "") or "")
    history = _normalise_history(cfg.get("token_history", []))
    if old_token:
        # Newest first — the page lists the most recent rotation at the top.
        history = ([_history_entry(old_token)] + history)[:_KEEP_HISTORY]

    # token_urlsafe(32), the same mint as POST /api/tokens/rotate: both
    # endpoints must produce interchangeable tokens and history rows.
    new_token = secrets.token_urlsafe(32)
    cfg["mcp_auth_token"] = new_token
    cfg["token_history"] = history
    await store.save(cfg)

    # The plaintext token is returned exactly once, on the response to the
    # rotation that minted it; every later read is masked.
    return {
        "status": "ok",
        "token": new_token,
        "masked": mask_secret(new_token),
        "history": history,
        "message": "Token rotated. Save it — shown only once.",
    }


@router.get("/mcp/status", response_model=McpProcessStatus)
async def mcp_status() -> McpProcessStatus:
    """Return MCP server process status."""
    pm = get_process_manager()
    info = await pm.status()
    return McpProcessStatus(**info)


@router.post("/mcp/restart")
async def mcp_restart() -> dict[str, str]:
    """Restart the MCP server process."""
    pm = get_process_manager()
    ok = await pm.restart()
    if ok:
        return {"status": "ok", "message": "MCP server restarted"}
    if pm.is_running:
        # Alive but not yet accepting connections: the connector answers 502
        # for another few seconds, so do not claim success.
        return {"status": "partial", "message": "MCP server restarted but not yet accepting connections"}
    return {"status": "error", "message": "Failed to restart MCP server — check command config"}
