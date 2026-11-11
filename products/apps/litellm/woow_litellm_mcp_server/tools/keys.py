"""Virtual-key management tools (category: keys)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..deps import litellm_client
from ..errors import ToolError
from ..gating import ToolGate
from ..registry import OP_CREATE, OP_DELETE, OP_UPDATE
from ._common import destructive, path_segment, prune_none, read_only, writing

# Fields /key/update accepts that a caller may legitimately want to RESET to
# null. prune_none() can never transmit a null, so "clear" is the only way out
# of a state like max_budget=7.25 once it has been set.
_CLEARABLE_KEY_FIELDS = frozenset(
    {
        "models",
        "max_budget",
        "tpm_limit",
        "rpm_limit",
        "budget_duration",
        "metadata",
        "duration",
        "key_alias",
        "user_id",
        "team_id",
    }
)


def register(mcp: Any, gate: ToolGate) -> None:
    if gate.is_tool_enabled("litellm_generate_key"):

        @mcp.tool(
            name="litellm_generate_key",
            annotations=writing("Generate key"),
        )
        async def litellm_generate_key(
            ctx: Context,
            key_alias: str | None = None,
            models: list[str] | None = None,
            max_budget: float | None = None,
            user_id: str | None = None,
            team_id: str | None = None,
            duration: str | None = None,
            tpm_limit: int | None = None,
            rpm_limit: int | None = None,
            budget_duration: str | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> dict:
            """Create a virtual key.

            All parameters are optional; ``duration`` accepts strings like
            ``"30d"`` and ``budget_duration`` resets the budget on that cadence.
            """
            gate.require_operation("litellm_generate_key", OP_CREATE)
            body = prune_none(
                {
                    "key_alias": key_alias,
                    "models": models,
                    "max_budget": max_budget,
                    "user_id": user_id,
                    "team_id": team_id,
                    "duration": duration,
                    "tpm_limit": tpm_limit,
                    "rpm_limit": rpm_limit,
                    "budget_duration": budget_duration,
                    "metadata": metadata,
                }
            )
            return await litellm_client(ctx).post(
                "/key/generate", json_data=body
            )

    if gate.is_tool_enabled("litellm_list_keys"):

        @mcp.tool(
            name="litellm_list_keys",
            annotations=read_only("List keys"),
        )
        async def litellm_list_keys(
            ctx: Context,
            page: int = 1,
            size: int = 50,
            user_id: str | None = None,
            team_id: str | None = None,
            key_alias: str | None = None,
            return_full_object: bool = True,
        ) -> dict:
            """List/paginate virtual keys."""
            params = prune_none(
                {
                    "page": page,
                    "size": size,
                    "user_id": user_id,
                    "team_id": team_id,
                    "key_alias": key_alias,
                    "return_full_object": return_full_object,
                }
            )
            return await litellm_client(ctx).get("/key/list", params=params)

    if gate.is_tool_enabled("litellm_key_info"):

        @mcp.tool(
            name="litellm_key_info",
            annotations=read_only("Key info"),
        )
        async def litellm_key_info(ctx: Context, key: str) -> dict:
            """Get spend, budget and allowed models for one key."""
            return await litellm_client(ctx).get(
                "/key/info", params={"key": key}
            )

    if gate.is_tool_enabled("litellm_update_key"):

        @mcp.tool(
            name="litellm_update_key",
            annotations=writing("Update key"),
        )
        async def litellm_update_key(
            ctx: Context,
            key: str,
            key_alias: str | None = None,
            models: list[str] | None = None,
            max_budget: float | None = None,
            tpm_limit: int | None = None,
            rpm_limit: int | None = None,
            budget_duration: str | None = None,
            duration: str | None = None,
            user_id: str | None = None,
            team_id: str | None = None,
            blocked: bool | None = None,
            metadata: dict[str, Any] | None = None,
            clear: list[str] | None = None,
        ) -> dict:
            """Update key tunables (alias, expiry, ownership, budget, limits).

            Omitted arguments are left unchanged (nulls are never sent). To
            RESET a field to "unlimited"/unset, name it in ``clear`` — e.g.
            ``clear=["max_budget", "budget_duration"]`` — which is the only way
            to undo a budget once applied. ``duration`` moves the expiry
            (``"30d"``); ``clear=["duration"]`` makes the key non-expiring.
            """
            gate.require_operation("litellm_update_key", OP_UPDATE)
            body = prune_none(
                {
                    "key": key,
                    "key_alias": key_alias,
                    "models": models,
                    "max_budget": max_budget,
                    "tpm_limit": tpm_limit,
                    "rpm_limit": rpm_limit,
                    "budget_duration": budget_duration,
                    "duration": duration,
                    "user_id": user_id,
                    "team_id": team_id,
                    "blocked": blocked,
                    "metadata": metadata,
                }
            )
            # Applied AFTER prune_none: an explicit null is the payload here, so
            # it must survive the pruning pass that exists to stop accidental
            # nulls from wiping fields the caller never mentioned.
            for field in clear or []:
                name = str(field).strip()
                if name not in _CLEARABLE_KEY_FIELDS:
                    raise ToolError(
                        f"clear=['{name}'] is not a resettable field. "
                        f"Choose from: {sorted(_CLEARABLE_KEY_FIELDS)}."
                    )
                body[name] = None
            return await litellm_client(ctx).post("/key/update", json_data=body)

    if gate.is_tool_enabled("litellm_delete_key"):

        @mcp.tool(
            name="litellm_delete_key",
            annotations=destructive("Delete key"),
        )
        async def litellm_delete_key(
            ctx: Context,
            keys: list[str] | None = None,
            key_aliases: list[str] | None = None,
        ) -> dict:
            """[DESTRUCTIVE] Delete keys by ``keys[]`` or ``key_aliases[]``.

            Supply at least one of the two lists. Deleted keys stop working
            immediately and cannot be recovered.
            """
            gate.require_operation("litellm_delete_key", OP_DELETE)
            # Fail fast at the tool boundary. Without this, prune_none() strips
            # both selectors and we POST an empty {} to the bulk-delete
            # endpoint, betting the user's entire key set on LiteLLM rejecting
            # an empty selector. That is not a bet this tool should place.
            if not keys and not key_aliases:
                raise ToolError(
                    "litellm_delete_key requires a non-empty keys[] or "
                    "key_aliases[] list; refusing to send an empty selector to "
                    "the bulk-delete endpoint."
                )
            body = prune_none({"keys": keys, "key_aliases": key_aliases})
            return await litellm_client(ctx).post("/key/delete", json_data=body)

    if gate.is_tool_enabled("litellm_block_key"):

        @mcp.tool(
            name="litellm_block_key",
            annotations=destructive("Block key"),
        )
        async def litellm_block_key(ctx: Context, key: str) -> dict:
            """[DESTRUCTIVE] Block a key from making requests.

            Reversible with ``litellm_unblock_key``.
            """
            gate.require_operation("litellm_block_key", OP_UPDATE)
            return await litellm_client(ctx).post(
                "/key/block", json_data={"key": key}
            )

    if gate.is_tool_enabled("litellm_unblock_key"):

        @mcp.tool(
            name="litellm_unblock_key",
            annotations=writing("Unblock key"),
        )
        async def litellm_unblock_key(ctx: Context, key: str) -> dict:
            """Re-enable a previously blocked key."""
            gate.require_operation("litellm_unblock_key", OP_UPDATE)
            return await litellm_client(ctx).post(
                "/key/unblock", json_data={"key": key}
            )

    if gate.is_tool_enabled("litellm_regenerate_key"):

        @mcp.tool(
            name="litellm_regenerate_key",
            # Rotation is the ONE irreversible key operation in this family, so
            # it must carry destructiveHint=True: MCP clients and the admin GUI
            # decide whether to prompt for confirmation from that flag, and it
            # previously said "not destructive" while block_key (fully
            # reversible via unblock_key) said it was.
            annotations=destructive("Regenerate key"),
        )
        async def litellm_regenerate_key(
            ctx: Context,
            key: str,
            new_master_key: str | None = None,
        ) -> dict:
            """[DESTRUCTIVE] Rotate the secret of an existing key.

            The key's config is preserved and a NEW key value is returned; the
            old value stops working immediately and every consumer still holding
            it breaks. This cannot be undone — confirm intent before calling.
            """
            gate.require_operation("litellm_regenerate_key", OP_UPDATE)
            body = prune_none({"new_master_key": new_master_key})
            return await litellm_client(ctx).post(
                f"/key/{path_segment(key, 'key')}/regenerate", json_data=body
            )
