"""Strict task and wire value types; validation never echoes supplied values."""
from __future__ import annotations

from datetime import datetime
import ipaddress
import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictStr


class Arguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


def _id(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value) or int(value) > 2**64 - 1:
        raise ValueError("Expected a positive uint64 decimal string")
    return value


def _timestamp(value: str) -> str:
    if len(value) > 40 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("Expected an RFC3339 timestamp with timezone")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _route(value: str) -> str:
    if "/" not in value or "%" in value:
        raise ValueError("Expected a CIDR")
    return str(ipaddress.ip_network(value, strict=False))


def _username(value: str) -> str:
    if value in (".", ".."):
        raise ValueError("Dot path segments are not supported")
    return value


def _tag(value: str) -> str:
    if value.lower() != value:
        raise ValueError("Tags must be lowercase")
    return value


Id = Annotated[StrictStr, AfterValidator(_id)]
Timestamp = Annotated[StrictStr, AfterValidator(_timestamp)]
# v0.29.4 grpcv1.CreateUser/RenameUser -> state.CreateUser/UpdateUser do
# NOT call util.ValidateUsername or db.RenameUser. Do not apply their name or
# OIDC restrictions here. Preserve case, Unicode and punctuation literally.
# The 2048-character cap and excluded controls/path delimiters are MCP safety
# limits, not upstream syntax rules. quote(..., safe="") still encodes paths.
Username = Annotated[StrictStr, Field(
    min_length=1, max_length=2048, pattern=r"^[^\x00-\x1f\x7f-\x9f/\\%?#]+$",
    json_schema_extra={"not": {"enum": [".", ".."]}}), AfterValidator(_username)]
NodeName = Annotated[StrictStr, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
AuthId = Annotated[StrictStr, Field(pattern=r"^hskey-authreq-[A-Za-z0-9_-]{24}$")]
Prefix = Annotated[StrictStr, Field(pattern=r"^(?:[A-Za-z0-9_-]{7}(?:\*\*\*)?|[A-Za-z0-9_-]{12}|hskey-api-[A-Za-z0-9_-]{12}(?:-\*\*\*)?)$")]
# grpcv1.validateTag requires a lowercase tag: value without spaces. Policy
# Tag.Validate additionally requires an ASCII letter first, but CreatePreAuthKey
# does not call it. Leave that and policy existence/ownership to the backend.
# Length/control/whitespace bounds are local safety limits (the upstream Fields
# check even tolerates trailing whitespace); punctuation/Unicode are retained.
Tag = Annotated[StrictStr, Field(
    max_length=2048, pattern=r"^tag:[^A-Z\s\x00-\x1f\x7f-\x9f]*$"), AfterValidator(_tag)]
Tags = Annotated[list[Tag], Field(max_length=128)]
NonemptyTags = Annotated[list[Tag], Field(min_length=1, max_length=128)]
Route = Annotated[StrictStr, Field(max_length=64), AfterValidator(_route)]
Routes = Annotated[list[Route], Field(max_length=256)]
PolicyText = Annotated[StrictStr, Field(min_length=1, max_length=262144)]
Digest = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
Text = Annotated[StrictStr, Field(max_length=2048)]
Bool = StrictBool

