"""Runtime settings, read from ``NEXTCLOUD_MCP_*`` environment variables (and ``.env``)."""

from __future__ import annotations

import ssl
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "NEXTCLOUD_MCP_"


class SettingsError(Exception):
    """Start-up configuration problem. The message never contains a secret value."""


class Settings(BaseSettings):
    """Settings of one server process (one Nextcloud account)."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    base_url: str = Field(description="Nextcloud root URL.")
    username: str = Field(description="Login name of the account.")
    app_password: SecretStr = Field(description="App password of the account.")
    readonly: bool = True
    allow_delete: bool = False
    disabled_tools: str = ""
    request_timeout: float = Field(default=30.0, gt=0, le=3600)
    max_text_bytes: int = Field(default=1_048_576, ge=1)
    max_upload_bytes: int = Field(default=10_485_760, ge=1)
    tree_max_entries: int = Field(default=500, ge=1)
    verify_tls: bool = True
    ca_bundle: str | None = None
    allowed_hosts: str = ""
    error_codes: str = "auto"

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
            raise ValueError("must not contain spaces or control characters")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            raise ValueError("must start with https:// or http://")
        if "@" in parts.netloc:
            raise ValueError("must not contain a user name or password")
        if "?" in value or "#" in value:
            raise ValueError("must not contain a query or fragment")
        if not parts.hostname:
            raise ValueError("must contain a host name")
        try:
            _ = parts.port
        except ValueError:
            raise ValueError("has an invalid port") from None
        path = parts.path.rstrip("/")
        return f"{parts.scheme}://{parts.netloc}{path}"

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value) or ":" in value:
            raise ValueError("must not contain control characters or ':'")
        return value

    @field_validator("app_password")
    @classmethod
    def _check_password(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.strip():
            raise ValueError("must not be empty")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
            raise ValueError("must not contain control characters")
        return value

    @field_validator("error_codes")
    @classmethod
    def _check_error_codes(cls, value: str) -> str:
        aliases = {"1": "true", "yes": "true", "on": "true", "0": "false", "no": "false"}
        normalized = value.strip().lower()
        normalized = aliases.get(normalized, "false" if normalized == "off" else normalized)
        if normalized not in ("auto", "true", "false"):
            raise ValueError("must be auto, true or false")
        return normalized

    @field_validator("disabled_tools")
    @classmethod
    def _check_disabled_tools(cls, value: str) -> str:
        from .tools import ALL_TOOLS  # local import: tools imports this module

        unknown = sorted(set(_split(value)) - set(ALL_TOOLS))
        if unknown:
            raise ValueError(f"unknown tool name(s) {', '.join(unknown)}")
        return value

    @field_validator("ca_bundle")
    @classmethod
    def _check_ca_bundle(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        path = Path(value.strip()).expanduser()
        if not path.is_file():
            raise ValueError("is not a readable file")
        try:
            ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError):
            raise ValueError("is not a valid PEM CA bundle") from None
        return str(path)

    @field_validator("allowed_hosts")
    @classmethod
    def _check_allowed_hosts(cls, value: str) -> str:
        for host in _split(value):
            if any(ch in host for ch in "/@?# ") or any(ord(ch) < 0x21 for ch in host):
                raise ValueError("must be host names (or patterns like *.example.com) only")
        return value

    @property
    def disabled_tool_names(self) -> frozenset[str]:
        """Names from ``DISABLED_TOOLS`` (comma separated, blanks ignored)."""
        return frozenset(_split(self.disabled_tools))

    @property
    def allowed_host_names(self) -> list[str]:
        """Extra Host header values accepted by the HTTP/SSE transports."""
        return _split(self.allowed_hosts)

    def tls_verify(self) -> bool | ssl.SSLContext:
        """Value for httpx ``verify``: False, a context for CA_BUNDLE, or True."""
        if not self.verify_tls:
            return False
        if self.ca_bundle:
            return ssl.create_default_context(cafile=self.ca_bundle)
        return True

    def startup_warnings(self) -> list[str]:
        """Risky but allowed settings, logged once at start-up (no secrets)."""
        warnings = []
        if not self.verify_tls:
            warnings.append(
                "NEXTCLOUD_MCP_VERIFY_TLS=false: TLS certificates are not verified; prefer "
                "NEXTCLOUD_MCP_CA_BUNDLE for a private CA."
            )
        if self.base_url.startswith("http://"):
            warnings.append(
                "NEXTCLOUD_MCP_BASE_URL uses http://: the app password is sent unencrypted; "
                "use this only on a trusted network."
            )
        return warnings


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _describe(error: ValidationError) -> str:
    """One line describing the first problem, naming variables but never values."""
    problems = []
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        field = str(item["loc"][0]).upper() if item.get("loc") else "settings"
        if item.get("type") == "missing":
            problems.append(f"{ENV_PREFIX}{field} is required")
        else:
            message = str(item.get("msg", "is invalid"))
            message = message.removeprefix("Value error, ")
            problems.append(f"{ENV_PREFIX}{field}: {message}")
    return "; ".join(problems) or "invalid settings"


def load_settings(**overrides: object) -> Settings:
    """Load settings from the environment, raising :class:`SettingsError` on problems."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        raise SettingsError(_describe(exc)) from None
