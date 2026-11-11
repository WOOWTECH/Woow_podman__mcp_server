"""Team management tools (category: teams)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..deps import litellm_client
from ..gating import ToolGate
from ..registry import OP_CREATE, OP_DELETE, OP_UPDATE
from ._common import destructive, prune_none, read_only, writing


def register(mcp: Any, gate: ToolGate) -> None:
    if gate.is_tool_enabled("litellm_create_team"):

        @mcp.tool(
            name="litellm_create_team",
            annotations=writing("Create team"),
        )
        async def litellm_create_team(
            ctx: Context,
            team_alias: str,
            models: list[str] | None = None,
            max_budget: float | None = None,
            tpm_limit: int | None = None,
            rpm_limit: int | None = None,
            members_with_roles: list[dict[str, Any]] | None = None,
            organization_id: str | None = None,
        ) -> dict:
            """Create a team."""
            gate.require_operation("litellm_create_team", OP_CREATE)
            body = prune_none(
                {
                    "team_alias": team_alias,
                    "models": models,
                    "max_budget": max_budget,
                    "tpm_limit": tpm_limit,
                    "rpm_limit": rpm_limit,
                    "members_with_roles": members_with_roles,
                    "organization_id": organization_id,
                }
            )
            return await litellm_client(ctx).post("/team/new", json_data=body)

    if gate.is_tool_enabled("litellm_list_teams"):

        @mcp.tool(
            name="litellm_list_teams",
            annotations=read_only("List teams"),
        )
        async def litellm_list_teams(
            ctx: Context,
            page: int = 1,
            page_size: int = 50,
            team_alias: str | None = None,
            user_id: str | None = None,
            organization_id: str | None = None,
        ) -> dict:
            """List/paginate teams.

            ``page_size`` matches ``litellm_list_users``; the wire name
            /v2/team/list expects is ``page_size`` too. Sending ``size`` (the
            old spelling) made FastAPI ignore it and silently fall back to 10
            results, so an agent enumerating teams saw only the first page and
            concluded the rest did not exist.
            """
            params = prune_none(
                {
                    "page": page,
                    "page_size": page_size,
                    "team_alias": team_alias,
                    "user_id": user_id,
                    "organization_id": organization_id,
                }
            )
            return await litellm_client(ctx).get("/v2/team/list", params=params)

    if gate.is_tool_enabled("litellm_team_info"):

        @mcp.tool(
            name="litellm_team_info",
            annotations=read_only("Team info"),
        )
        async def litellm_team_info(ctx: Context, team_id: str) -> dict:
            """Get one team's members, budget and spend by team_id."""
            return await litellm_client(ctx).get(
                "/team/info", params={"team_id": team_id}
            )

    if gate.is_tool_enabled("litellm_update_team"):

        @mcp.tool(
            name="litellm_update_team",
            annotations=writing("Update team"),
        )
        async def litellm_update_team(
            ctx: Context,
            team_id: str,
            team_alias: str | None = None,
            models: list[str] | None = None,
            max_budget: float | None = None,
            tpm_limit: int | None = None,
            rpm_limit: int | None = None,
        ) -> dict:
            """Update team tunables by team_id."""
            gate.require_operation("litellm_update_team", OP_UPDATE)
            body = prune_none(
                {
                    "team_id": team_id,
                    "team_alias": team_alias,
                    "models": models,
                    "max_budget": max_budget,
                    "tpm_limit": tpm_limit,
                    "rpm_limit": rpm_limit,
                }
            )
            return await litellm_client(ctx).post("/team/update", json_data=body)

    if gate.is_tool_enabled("litellm_delete_team"):

        @mcp.tool(
            name="litellm_delete_team",
            annotations=destructive("Delete team"),
        )
        async def litellm_delete_team(ctx: Context, team_ids: list[str]) -> dict:
            """[DESTRUCTIVE] Delete teams by ``team_ids[]``.

            Removes the teams and their membership associations permanently.
            """
            gate.require_operation("litellm_delete_team", OP_DELETE)
            return await litellm_client(ctx).post(
                "/team/delete", json_data={"team_ids": team_ids}
            )

    if gate.is_tool_enabled("litellm_team_member_add"):

        @mcp.tool(
            name="litellm_team_member_add",
            annotations=writing("Add team member"),
        )
        async def litellm_team_member_add(
            ctx: Context,
            team_id: str,
            user_id: str | None = None,
            user_email: str | None = None,
            role: str = "user",
            max_budget_in_team: float | None = None,
        ) -> dict:
            """Add a member to a team.

            Identify the member by ``user_id`` or ``user_email``; ``role`` is
            typically ``"user"`` or ``"admin"``.
            """
            gate.require_operation("litellm_team_member_add", OP_CREATE)
            member = prune_none(
                {
                    "user_id": user_id,
                    "user_email": user_email,
                    "role": role,
                }
            )
            body = prune_none(
                {
                    "team_id": team_id,
                    "member": member,
                    "max_budget_in_team": max_budget_in_team,
                }
            )
            return await litellm_client(ctx).post(
                "/team/member_add", json_data=body
            )

    if gate.is_tool_enabled("litellm_team_member_delete"):

        @mcp.tool(
            name="litellm_team_member_delete",
            annotations=destructive("Remove team member"),
        )
        async def litellm_team_member_delete(
            ctx: Context,
            team_id: str,
            user_id: str | None = None,
            user_email: str | None = None,
        ) -> dict:
            """[DESTRUCTIVE] Remove a member from a team.

            Identify the member by ``user_id`` or ``user_email``.
            """
            gate.require_operation("litellm_team_member_delete", OP_DELETE)
            body = prune_none(
                {
                    "team_id": team_id,
                    "user_id": user_id,
                    "user_email": user_email,
                }
            )
            return await litellm_client(ctx).post(
                "/team/member_delete", json_data=body
            )
