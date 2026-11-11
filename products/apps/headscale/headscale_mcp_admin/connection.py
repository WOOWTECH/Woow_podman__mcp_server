"""Headscale connection editing: explicit credential actions and saved revisions."""
from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from mcp_admin_core.config import get_config_store
from mcp_admin_core.configuration import configuration_status, revision_header, run_configuration_transition
from mcp_admin_core.process import get_process_manager

router = APIRouter(prefix="/api/headscale/connection", tags=["Headscale setup"])
PUBLIC_FIELDS = ("base_url", "enrollment_url", "headplane_url", "paas_base_url", "workspace_id", "headscale_service_id")
SECRET_FIELDS = ("api_key", "paas_api_token")


def normalize_url(value: str, *, api: bool = False) -> str:
    """Only administrative connection roots, never credentials or arbitrary paths."""
    value = value.strip()
    if not value:
        return ""
    try:
        u = urlsplit(value)
        if (u.scheme not in ("http", "https") or not u.hostname or u.username is not None
                or u.password is not None or u.query or u.fragment or "\\" in value or "%" in value
                or any(ord(c) <= 32 or ord(c) == 127 for c in value) or u.port == 0
                or any(p in (".", "..") for p in u.path.split("/"))):
            raise ValueError()
        path = u.path.rstrip("/")
        if api:
            if path == "/api/v1":
                path = ""
            elif path:
                raise ValueError()
        return urlunsplit((u.scheme, u.netloc, path, "", ""))
    except (TypeError, ValueError):
        raise HTTPException(422, "HEADSCALE_INVALID_URL") from None


class CredentialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    action: Literal["retain", "replace", "clear"] = "retain"
    value: SecretStr | None = None

    @model_validator(mode="after")
    def validate_action(self):
        value = self.value.get_secret_value() if self.value is not None else ""
        if self.action == "replace":
            if not value or len(value) > 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
                raise ValueError("Invalid credential replacement")
        elif self.value is not None:
            raise ValueError("Only replacement accepts a credential value")
        return self


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    base_url: str = Field(max_length=2048)
    enrollment_url: str = Field(default="", max_length=2048)
    headplane_url: str = Field(default="", max_length=2048)
    paas_base_url: str = Field(default="", max_length=2048)
    workspace_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{0,17})?$")
    headscale_service_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{0,17})?$")
    api_key: CredentialUpdate = Field(default_factory=CredentialUpdate)
    paas_api_token: CredentialUpdate = Field(default_factory=CredentialUpdate)
    apply: bool = True


@router.get("")
async def read_connection(response: Response):
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get("connection") or {}
    return {"configuration": configuration,
            "connection": {k: conn.get(k, "") for k in PUBLIC_FIELDS},
            "credentials": {k: {"set": bool(conn.get(k))} for k in SECRET_FIELDS},
            "verification": "not_checked", "organization_mode": "single_organization_admin"}


@router.put("")
async def save_connection(payload: ConnectionUpdate, request: Request, response: Response,
                          if_match: Annotated[str | None, Header(alias="If-Match")] = None):
    updates = {k: getattr(payload, k) for k in PUBLIC_FIELDS}
    updates["base_url"] = normalize_url(payload.base_url, api=True)
    for key in ("enrollment_url", "headplane_url", "paas_base_url"):
        updates[key] = normalize_url(updates[key])
    if updates["paas_base_url"] and urlsplit(updates["paas_base_url"]).path:
        raise HTTPException(422, "PAAS_ROOT_URL_REQUIRED")
    configured = [bool(updates[k]) for k in ("paas_base_url", "workspace_id", "headscale_service_id")]
    if any(configured) and not all(configured):
        raise HTTPException(422, "PAAS_BINDING_INCOMPLETE")

    async def update():
        for key in SECRET_FIELDS:
            change = getattr(payload, key)
            if change.action == "replace":
                updates[key] = change.value.get_secret_value()
            elif change.action == "clear":
                updates[key] = ""
        # Changing an origin must never silently forward its previous credential.
        old = await get_config_store().get("connection", {}) or {}
        for origin, secret in (("base_url", "api_key"), ("paas_base_url", "paas_api_token")):
            if old.get(origin) and old.get(origin) != updates[origin] and old.get(secret):
                if getattr(payload, secret).action == "retain":
                    raise HTTPException(422, "CREDENTIAL_REPLACEMENT_REQUIRED_FOR_NEW_ORIGIN")
        merged = await get_config_store().patch("connection", updates)
        from .log_redaction import set_secrets
        set_secrets(*(merged.get(k, "") for k in SECRET_FIELDS))
        applied = False
        if payload.apply:
            try:
                applied = bool(await get_process_manager().start_or_restart())
            except Exception:
                pass  # The configuration was saved; apply and API verification are separate facts.
        return {"saved": True, "applied": applied, "verification": "not_checked"}

    result = await run_configuration_transition(get_config_store(), get_process_manager(), if_match,
                                                 update, response, request=request)
    # Process spawning acknowledges a request; only the canonical verifier marks
    # the saved revision as applied after checking the running child.
    result["applied"] = result["configuration"]["apply_state"] == "applied"
    return result
