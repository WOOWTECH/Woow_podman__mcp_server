"""Runtime configuration, sourced from EMQX_MCP_* environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMQX_MCP_", extra="ignore")

    base_url: str = Field(
        "",
        description="EMQX dashboard base URL; /api/v5 is appended automatically.",
    )
    api_key: str = ""
    api_secret: str = ""

    readonly: bool = Field(
        False, description="When true, no write or destructive tool is registered."
    )
    disabled_categories: str = Field("", description="Comma-separated category names.")
    disabled_tools: str = Field("", description="Comma-separated tool names.")
    disabled_operations: str = Field(
        "", description='JSON object, e.g. {"emqx_manage_authn_users": ["delete"]}.'
    )

    default_limit: int = 50
    max_limit: int = 200
    request_timeout: float = 30.0


settings = Settings()
