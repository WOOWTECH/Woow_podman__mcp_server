"""Pydantic response models per tool family.

These give each read-oriented tool a stable ``outputSchema`` and let the test
suite assert on shapes without coupling to LiteLLM's exact (and version-drifting)
payloads. They are deliberately permissive (``extra='allow'``) so unexpected
fields from a newer LiteLLM pass through untouched.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class ModelInfo(_Base):
    """One entry from /v1/models or /model/info."""

    id: str | None = None
    model_name: str | None = None
    litellm_params: dict[str, Any] | None = None
    model_info: dict[str, Any] | None = None


class ModelList(_Base):
    object: str | None = None
    data: list[ModelInfo] = Field(default_factory=list)


class KeyInfo(_Base):
    """A virtual key record from /key/info or /key/list."""

    token: str | None = None
    key_name: str | None = None
    key_alias: str | None = None
    spend: float | None = None
    max_budget: float | None = None
    models: list[str] | None = None
    user_id: str | None = None
    team_id: str | None = None
    blocked: bool | None = None


class TeamInfo(_Base):
    team_id: str | None = None
    team_alias: str | None = None
    spend: float | None = None
    max_budget: float | None = None
    models: list[str] | None = None
    members_with_roles: list[dict[str, Any]] | None = None


class UserInfo(_Base):
    user_id: str | None = None
    user_email: str | None = None
    user_role: str | None = None
    spend: float | None = None
    max_budget: float | None = None
    teams: list[str] | None = None
    models: list[str] | None = None


class SpendRow(_Base):
    """A single per-request spend log row from /spend/logs."""

    request_id: str | None = None
    api_key: str | None = None
    user: str | None = None
    model: str | None = None
    spend: float | None = None
    total_tokens: int | None = None
    startTime: str | None = None
    endTime: str | None = None


class SpendReportRow(_Base):
    """A grouped row from /global/spend/report."""

    group_by_day: str | None = None
    total_spend: float | None = None
    api_key: str | None = None
    team_id: str | None = None
    user_id: str | None = None
    customer: str | None = None


class HealthStatus(_Base):
    """Result of /health or /health/readiness."""

    status: str | None = None
    healthy_endpoints: list[dict[str, Any]] | None = None
    unhealthy_endpoints: list[dict[str, Any]] | None = None
    healthy_count: int | None = None
    unhealthy_count: int | None = None
    db: str | None = None
    cache: dict[str, Any] | None = None


class PluginInfo(_Base):
    """A Claude-Code skill-hub plugin entry.

    ``source`` is an OBJECT upstream (``{"source": "github", "repo": "org/repo"}``),
    not a string: declaring it ``str`` made ``model_validate`` raise on every
    real gateway record. A bare string is still accepted and normalised so old
    captured payloads (and the string form ``register_plugin`` used to send)
    keep validating.
    """

    name: str | None = None
    source: dict[str, str] | None = None
    id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    version: str | None = None
    description: str | None = None
    category: str | None = None
    domain: str | None = None
    namespace: str | None = None
    enabled: bool | None = None

    @field_validator("source", mode="before")
    @classmethod
    def _coerce_source(cls, value: Any) -> Any:
        """Accept the legacy bare-string source, mirroring register_plugin."""
        if isinstance(value, str):
            return {"source": value}
        return value
