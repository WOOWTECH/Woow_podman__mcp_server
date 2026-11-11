"""Internal-user management tools (category: users)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..deps import litellm_client
from ..gating import ToolGate
from ..registry import OP_CREATE, OP_DELETE, OP_UPDATE
from ._common import destructive, prune_none, read_only, writing


def register(mcp: Any, gate: ToolGate) -> None:
    if gate.is_tool_enabled("litellm_create_user"):

        @mcp.tool(
            name="litellm_create_user",
            annotations=writing("Create user"),
        )
        async def litellm_create_user(
            ctx: Context,
            user_email: str | None = None,
            user_id: str | None = None,
            user_alias: str | None = None,
            user_role: str | None = None,
            teams: list[str] | None = None,
            models: list[str] | None = None,
            max_budget: float | None = None,
            auto_create_key: bool = True,
        ) -> dict:
            """Create an internal user.

            ``user_role`` is one of LiteLLM's roles (e.g. ``internal_user``,
            ``proxy_admin``). ``user_alias`` is the human-readable display name.
            When ``auto_create_key`` is true a key is minted.

            ``teams`` IS honoured here (NewUserRequest accepts it) — unlike on
            ``litellm_update_user``, where LiteLLM has no such field.
            """
            gate.require_operation("litellm_create_user", OP_CREATE)
            body = prune_none(
                {
                    "user_email": user_email,
                    "user_id": user_id,
                    "user_alias": user_alias,
                    "user_role": user_role,
                    "teams": teams,
                    "models": models,
                    "max_budget": max_budget,
                    "auto_create_key": auto_create_key,
                }
            )
            return await litellm_client(ctx).post("/user/new", json_data=body)

    if gate.is_tool_enabled("litellm_list_users"):

        @mcp.tool(
            name="litellm_list_users",
            annotations=read_only("List users"),
        )
        async def litellm_list_users(
            ctx: Context,
            page: int = 1,
            page_size: int = 50,
            role: str | None = None,
            user_ids: list[str] | None = None,
            user_email: str | None = None,
            team: str | None = None,
            sort_by: str | None = None,
            sort_order: str | None = None,
        ) -> dict:
            """List/paginate users.

            Push filtering down to the gateway rather than paging the whole
            directory: ``user_email`` finds one user, ``team`` lists a team's
            members. ``sort_by`` names a column (e.g. ``spend``) and
            ``sort_order`` is ``asc``/``desc``.
            """
            params = prune_none(
                {
                    "page": page,
                    "page_size": page_size,
                    "role": role,
                    "user_ids": ",".join(user_ids) if user_ids else None,
                    "user_email": user_email,
                    "team": team,
                    "sort_by": sort_by,
                    "sort_order": sort_order,
                }
            )
            return await litellm_client(ctx).get("/user/list", params=params)

    if gate.is_tool_enabled("litellm_user_info"):

        @mcp.tool(
            name="litellm_user_info",
            annotations=read_only("User info"),
        )
        async def litellm_user_info(ctx: Context, user_id: str) -> dict:
            """Get one user's teams, keys, budget and spend by user_id."""
            return await litellm_client(ctx).get(
                "/user/info", params={"user_id": user_id}
            )

    if gate.is_tool_enabled("litellm_update_user"):

        @mcp.tool(
            name="litellm_update_user",
            annotations=writing("Update user"),
        )
        async def litellm_update_user(
            ctx: Context,
            user_id: str,
            user_role: str | None = None,
            user_alias: str | None = None,
            user_email: str | None = None,
            max_budget: float | None = None,
            models: list[str] | None = None,
            team_id: str | None = None,
        ) -> dict:
            """Update a user's role, alias, email, budget or models.

            To change TEAM MEMBERSHIP use ``litellm_team_member_add`` /
            ``litellm_team_member_delete``. LiteLLM's UpdateUserRequest has no
            ``teams`` field, so the ``teams`` argument this tool used to accept
            was silently discarded by pydantic and the call reported success
            while changing nothing. ``team_id`` (the single-team field
            UpdateUserRequest really has) is exposed instead.
            """
            gate.require_operation("litellm_update_user", OP_UPDATE)
            body = prune_none(
                {
                    "user_id": user_id,
                    "user_role": user_role,
                    "user_alias": user_alias,
                    "user_email": user_email,
                    "max_budget": max_budget,
                    "models": models,
                    "team_id": team_id,
                }
            )
            return await litellm_client(ctx).post("/user/update", json_data=body)

    if gate.is_tool_enabled("litellm_delete_user"):

        @mcp.tool(
            name="litellm_delete_user",
            annotations=destructive("Delete user"),
        )
        async def litellm_delete_user(ctx: Context, user_ids: list[str]) -> dict:
            """[DESTRUCTIVE] Delete users by ``user_ids[]``.

            Also removes the users' keys; the action is irreversible. Returns
            ``{"deleted": <rows>, "user_ids": [...]}``.
            """
            gate.require_operation("litellm_delete_user", OP_DELETE)
            result = await litellm_client(ctx).post(
                "/user/delete", json_data={"user_ids": user_ids}
            )
            # /user/delete answers with the BARE integer 1 (rows deleted), not
            # an object. Returning it straight through failed FastMCP's
            # `-> dict` output-schema validation, which turned a completed,
            # irreversible deletion into "Error calling tool
            # 'litellm_delete_user'" — the caller was told the users survived
            # while they and their keys were already gone. Wrap it so the
            # declared shape is honest.
            return {"deleted": result, "user_ids": user_ids}
