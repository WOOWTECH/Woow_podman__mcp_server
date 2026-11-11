"""Runtime settings for the LiteLLM MCP server.

All values are read from the environment with the ``LITELLM_MCP_`` prefix so
they line up with the connection section written by the admin console
(``litellm_mcp_base_url`` -> ``LITELLM_MCP_BASE_URL`` etc.) and with the k8s
Secret used by the git-clone deployment.

NEVER hard-code the master key here; it always comes from the environment.

Two load-bearing rules govern the gating fields below; both exist because this
class is constructed inside the MCP *child process* at import time, where a
failure is invisible (the child dies before it can log anything useful):

  1. ``NoDecode`` — the admin console writes the gating fields as JSON strings
     (``litellm_mcp_admin.store.env_from_tool_settings``). Without ``NoDecode``
     pydantic-settings tries to json-decode complex fields itself and *raises*
     on anything that is not valid JSON (e.g. the legacy CSV form), killing the
     process. With ``NoDecode`` the raw string reaches our own validator.
  2. The ``mode="before"`` validator NEVER raises. A malformed gate value must
     degrade to "nothing disabled" rather than take the whole server down; a
     bad switch in the GUI must not be able to brick the MCP child.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Mapping

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .urls import normalize_base_url


def _split_csv(value: object) -> list[str]:
    """Accept a comma-separated string or a list and normalise to a list."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


# Sentinel returned when a value that was clearly *meant* to be JSON is
# malformed. Such a value degrades to "nothing disabled" rather than being
# mis-read as a CSV entry (a truncated '["litellm_delete_key"' must not become
# the literal tool name '["litellm_delete_key').
_BAD_JSON = object()


def _maybe_json(value: str) -> Any:
    """Decode a JSON string; never raises.

    Returns the original string when it is plainly not JSON (the CSV form), or
    :data:`_BAD_JSON` when it opens like JSON but fails to parse.
    """
    text = value.strip()
    if not text or text[0] not in "[{\"":
        return value
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _BAD_JSON


def _coerce_str_list(value: object) -> list[str]:
    """Normalise a str-list env value, accepting (in order):

    ``None``/``""`` -> empty; an already-parsed list/tuple/set; a JSON string
    (``'["a","b"]'``, the form ``env_from_tool_settings`` writes); a CSV string
    (``'a,b'``, the legacy form). Anything else degrades to empty.
    """
    try:
        if value is None or value == "":
            return []
        if isinstance(value, (list, tuple, set)):
            return _split_csv(value)
        if isinstance(value, Mapping):
            # A mapping has no meaningful list form; treat its keys as the list.
            return _split_csv(list(value.keys()))
        if isinstance(value, str):
            decoded = _maybe_json(value)
            if decoded is _BAD_JSON:
                return []
            if isinstance(decoded, str):
                return _split_csv(decoded)
            return _coerce_str_list(decoded)
        return _split_csv(value)
    except Exception:  # pragma: no cover - defensive: must never kill the child
        return []


def _coerce_operations(value: object) -> list[str] | dict[str, list[str]]:
    """Normalise ``disabled_operations``, keeping BOTH accepted shapes.

    The admin GUI stores the mapping form ``{tool: [op, ...]}``; the legacy env
    form is a flat list of ``"tool:op"`` / ``"op"`` strings.
    ``gating._normalize_operations`` understands both, so we pass the shape
    through rather than lossily flattening it here.
    """
    try:
        if value is None or value == "":
            return []
        if isinstance(value, Mapping):
            return {
                str(tool): _split_csv(ops)
                for tool, ops in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return _split_csv(value)
        if isinstance(value, str):
            decoded = _maybe_json(value)
            if decoded is _BAD_JSON:
                return []
            if isinstance(decoded, str):
                return _split_csv(decoded)
            return _coerce_operations(decoded)
        return _split_csv(value)
    except Exception:  # pragma: no cover - defensive: must never kill the child
        return []


class Settings(BaseSettings):
    """Environment-driven configuration for the MCP server."""

    model_config = SettingsConfigDict(
        env_prefix="LITELLM_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- upstream connection -------------------------------------------------
    base_url: str = Field(
        default="http://localhost:4000",
        description="LiteLLM gateway root; origin /v1 is removed, prefixed mounts are preserved.",
    )
    master_key: str = Field(
        default="",
        description="LiteLLM master (or admin) key sent as a Bearer token.",
    )

    # --- gating --------------------------------------------------------------
    # NOTE: readonly drops every tool that has a non-``read`` operation, not
    # just the ones flagged ``dangerous`` (see gating.ToolGate.is_tool_enabled).
    readonly: bool = Field(
        default=False,
        description="When true, only read operations remain; active probes/calculations are also dropped.",
    )
    # ``NoDecode`` on all three: the value arrives as a JSON string and MUST be
    # parsed by our forgiving validator, not by pydantic-settings' strict one.
    disabled_categories: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Tool categories to disable entirely (e.g. 'chat,users').",
    )
    disabled_tools: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Individual tool names to disable (e.g. 'litellm_delete_key').",
    )
    disabled_operations: Annotated[
        dict[str, list[str]] | list[str], NoDecode
    ] = Field(
        default_factory=list,
        description=(
            "Operation gates: either the mapping form {tool: [op, ...]} written "
            "by the admin GUI, or a flat list of 'tool:op' / 'op' strings."
        ),
    )

    # --- paging / limits -----------------------------------------------------
    default_limit: int = Field(
        default=50, ge=1, description="Default page size for list tools."
    )
    max_limit: int = Field(
        default=500, ge=1, description="Maximum page size a caller may request."
    )
    request_timeout: float = Field(
        default=60.0, gt=0, description="Per-request HTTP timeout in seconds."
    )

    @field_validator("disabled_categories", "disabled_tools", mode="before")
    @classmethod
    def _coerce_str_list_field(cls, value: object) -> list[str]:
        return _coerce_str_list(value)

    @field_validator("disabled_operations", mode="before")
    @classmethod
    def _coerce_operations_field(
        cls, value: object
    ) -> list[str] | dict[str, list[str]]:
        return _coerce_operations(value)

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize_gateway_root(cls, value: object) -> object:
        if isinstance(value, str):
            return normalize_base_url(value)
        return value

    def clamp_limit(self, requested: int | None) -> int:
        """Clamp a caller-requested page size into [1, max_limit]."""
        if requested is None:
            return self.default_limit
        return max(1, min(int(requested), self.max_limit))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def __getattr__(name: str) -> Any:
    """Lazily materialise the module-level ``settings`` singleton.

    Importing this module must NEVER construct ``Settings()``: a bad env value
    would then explode during ``import``, before any handler exists to report
    it, and the MCP child process would die silently. ``settings`` is therefore
    resolved on first *attribute access* via the cached factory. Prefer
    ``get_settings()`` in new code — it is the testable entry point.
    """
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
