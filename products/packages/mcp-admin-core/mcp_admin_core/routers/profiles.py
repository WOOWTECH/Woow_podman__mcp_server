"""Authenticated profile lifecycle; no backend secret is returned by this API."""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..profiles import get_profile_registry

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    label: str = Field(min_length=1, max_length=80)


@router.get("")
async def list_profiles():
    return {"profiles": await get_profile_registry().list()}


@router.post("", status_code=201)
async def create_profile(body: ProfileInput):
    registry = get_profile_registry()
    result = await registry.create(body.label)
    with registry.activate(result["id"]):
        # The manager does not spawn a product child that still lacks its required connection fields.
        await registry.manager_for(result["id"]).start()
    return result


@router.patch("/{profile_id}")
async def rename_profile(profile_id: str, body: ProfileInput):
    return await get_profile_registry().rename(profile_id, body.label)


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: str, request: Request):
    await get_profile_registry().delete(profile_id)
    callback = getattr(request.app.state, "profile_deleted", None)
    if callback is not None:
        callback(profile_id)
    return Response(status_code=204)
