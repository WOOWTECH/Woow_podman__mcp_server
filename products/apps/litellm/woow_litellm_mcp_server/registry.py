"""The single source of truth for the LiteLLM MCP tool surface.

Every tool that the server exposes is declared here as a :class:`ToolSpec`.
The tool modules under :mod:`woow_litellm_mcp_server.tools` MUST register exactly
these names (enforced by ``tests/test_mcp_surface.py``), and the admin console /
gating layer reads this registry so the GUI can never name a nonexistent tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ToolCategory(str, Enum):
    """High-level grouping used by the gate and the admin GUI."""

    MODELS = "models"
    CHAT = "chat"
    KEYS = "keys"
    TEAMS = "teams"
    USERS = "users"
    SPEND = "spend"
    HEALTH = "health"
    PLUGINS = "plugins"


# CRUD-style operation labels used for operation-level gating.
OP_READ = "read"
OP_CREATE = "create"
OP_UPDATE = "update"
OP_DELETE = "delete"


@dataclass(frozen=True)
class ToolSpec:
    """Declarative description of a single MCP tool."""

    name: str
    category: ToolCategory
    description: str
    method: str
    path: str
    operations: tuple[str, ...] = field(default_factory=lambda: (OP_READ,))
    dangerous: bool = False


def _t(
    name: str,
    category: ToolCategory,
    method: str,
    path: str,
    description: str,
    *,
    operations: tuple[str, ...] = (OP_READ,),
    dangerous: bool = False,
) -> ToolSpec:
    """Terse helper to build a ToolSpec."""
    return ToolSpec(
        name=name,
        category=category,
        description=description,
        method=method,
        path=path,
        operations=operations,
        dangerous=dangerous,
    )


_M = ToolCategory.MODELS
_C = ToolCategory.CHAT
_K = ToolCategory.KEYS
_TE = ToolCategory.TEAMS
_U = ToolCategory.USERS
_S = ToolCategory.SPEND
_H = ToolCategory.HEALTH
_P = ToolCategory.PLUGINS


# These were formerly labelled read. Keep legacy read-denies effective when
# applying the conservative create policy for active work (see PARITY.md).
ACTIVE_OPERATION_TOOLS = frozenset({
    "litellm_health", "litellm_token_counter", "litellm_spend_calculate",
})


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    # --- models -----------------------------------------------------------
    _t("litellm_list_models", _M, "GET", "/v1/models",
       "List all models registered in the LiteLLM gateway."),
    _t("litellm_model_info", _M, "GET", "/model/info",
       "Full deployment detail per model (provider, litellm_params, model_info)."),
    _t("litellm_model_group_info", _M, "GET", "/model_group/info",
       "Aggregated per-model-group capabilities."),
    _t("litellm_add_model", _M, "POST", "/model/new",
       "Register a new model deployment.", operations=(OP_CREATE,)),
    _t("litellm_update_model", _M, "PATCH", "/model/{model_id}/update",
       "Update an existing model deployment.", operations=(OP_UPDATE,)),
    _t("litellm_delete_model", _M, "POST", "/model/delete",
       "[DESTRUCTIVE] Remove a model deployment by id.",
       operations=(OP_DELETE,), dangerous=True),
    # --- chat -------------------------------------------------------------
    _t("litellm_chat_completion", _C, "POST", "/v1/chat/completions",
       "Run an OpenAI-compatible chat completion.", operations=(OP_CREATE,)),
    _t("litellm_token_counter", _C, "POST", "/utils/token_counter",
       "Count tokens without inference; may fetch images/tokenizers. Requires active-operation permission.",
       operations=(OP_CREATE,)),
    # --- keys -------------------------------------------------------------
    _t("litellm_generate_key", _K, "POST", "/key/generate",
       "Create a virtual key.", operations=(OP_CREATE,)),
    _t("litellm_list_keys", _K, "GET", "/key/list",
       "List/paginate virtual keys."),
    _t("litellm_key_info", _K, "GET", "/key/info",
       "Get spend, budget and allowed models for one key."),
    _t("litellm_update_key", _K, "POST", "/key/update",
       "Update key tunables (budget, models, limits).", operations=(OP_UPDATE,)),
    _t("litellm_delete_key", _K, "POST", "/key/delete",
       "[DESTRUCTIVE] Delete keys by keys[] or key_aliases[].",
       operations=(OP_DELETE,), dangerous=True),
    _t("litellm_block_key", _K, "POST", "/key/block",
       "[DESTRUCTIVE] Block a key from making requests.",
       operations=(OP_UPDATE,), dangerous=True),
    _t("litellm_unblock_key", _K, "POST", "/key/unblock",
       "Re-enable a previously blocked key.", operations=(OP_UPDATE,)),
    # The key is a PATH SEGMENT, not a body field (tools/keys.py posts to
    # /key/{key}/regenerate). Keep this in sync — the admin Tool Manager and any
    # gateway-side allow-list are generated from spec.path.
    # Rotation is irreversible (every holder of the old secret breaks
    # immediately), so it is `dangerous` even though it is an UPDATE.
    _t("litellm_regenerate_key", _K, "POST", "/key/{key}/regenerate",
       "[DESTRUCTIVE] Rotate the secret of an existing key; the old value dies.",
       operations=(OP_UPDATE,), dangerous=True),
    # --- teams ------------------------------------------------------------
    _t("litellm_create_team", _TE, "POST", "/team/new",
       "Create a team.", operations=(OP_CREATE,)),
    _t("litellm_list_teams", _TE, "GET", "/v2/team/list",
       "List/paginate teams."),
    _t("litellm_team_info", _TE, "GET", "/team/info",
       "Get one team's members, budget and spend by team_id."),
    _t("litellm_update_team", _TE, "POST", "/team/update",
       "Update team tunables by team_id.", operations=(OP_UPDATE,)),
    _t("litellm_delete_team", _TE, "POST", "/team/delete",
       "[DESTRUCTIVE] Delete teams by team_ids[].",
       operations=(OP_DELETE,), dangerous=True),
    _t("litellm_team_member_add", _TE, "POST", "/team/member_add",
       "Add a member (user_id/user_email, role) to a team.",
       operations=(OP_CREATE,)),
    _t("litellm_team_member_delete", _TE, "POST", "/team/member_delete",
       "[DESTRUCTIVE] Remove a member from a team.",
       operations=(OP_DELETE,), dangerous=True),
    # --- users ------------------------------------------------------------
    _t("litellm_create_user", _U, "POST", "/user/new",
       "Create an internal user.", operations=(OP_CREATE,)),
    _t("litellm_list_users", _U, "GET", "/user/list",
       "List/paginate users."),
    _t("litellm_user_info", _U, "GET", "/user/info",
       "Get one user's teams, keys, budget and spend by user_id."),
    # NB: no `teams` — UpdateUserRequest has no such field (use
    # litellm_team_member_add/delete); passing it was a silent no-op.
    _t("litellm_update_user", _U, "POST", "/user/update",
       "Update user role/budget/models/alias/email.", operations=(OP_UPDATE,)),
    _t("litellm_delete_user", _U, "POST", "/user/delete",
       "[DESTRUCTIVE] Delete users by user_ids[].",
       operations=(OP_DELETE,), dangerous=True),
    # --- spend ------------------------------------------------------------
    _t("litellm_spend_logs", _S, "GET", "/spend/logs",
       "Fetch per-request spend logs."),
    # /global/spend/report is LiteLLM *Enterprise*-only and 400s on a community
    # gateway, so the tool is built on /spend/logs (client-side aggregation)
    # with /global/spend/{teams,keys} as fallbacks. spend.py owns that fan-out;
    # the primary endpoint is recorded here.
    _t("litellm_global_spend_report", _S, "GET", "/spend/logs",
       "Aggregated spend report grouped by team/key/user/customer/model "
       "(community-edition safe: aggregates /spend/logs, falls back to "
       "/global/spend/teams and /global/spend/keys)."),
    _t("litellm_spend_calculate", _S, "POST", "/spend/calculate",
       "Estimate cost without billing; tokenization may fetch images/tokenizers. Requires active-operation permission.",
       operations=(OP_CREATE,)),
    # --- health -----------------------------------------------------------
    _t("litellm_health", _H, "GET", "/health",
       "Active deployment probes may incur provider charges and save health history (optional model filter).",
       operations=(OP_CREATE,)),
    _t("litellm_health_readiness", _H, "GET", "/health/readiness",
       "Gateway readiness incl. DB/cache status."),
    # --- plugins (Claude-Code skill hub) ----------------------------------
    _t("litellm_list_plugins", _P, "GET", "/claude-code/plugins",
       "List Claude-Code skill-hub plugins."),
    _t("litellm_plugin_info", _P, "GET", "/claude-code/plugins/{plugin_name}",
       "Get one skill-hub plugin's record by name."),
    _t("litellm_register_plugin", _P, "POST", "/claude-code/plugins",
       "Register a skill-hub plugin (source is an object, e.g. "
       "{'source': 'github', 'repo': 'org/repo'}).", operations=(OP_CREATE,)),
    # Without a delete path a plugin registered over MCP can only be removed
    # out-of-band with the master key; disable_plugin is not a substitute
    # because a disabled plugin still owns its name.
    _t("litellm_delete_plugin", _P, "DELETE",
       "/claude-code/plugins/{plugin_name}",
       "[DESTRUCTIVE] Remove a registered skill-hub plugin by name.",
       operations=(OP_DELETE,), dangerous=True),
    _t("litellm_enable_plugin", _P, "POST",
       "/claude-code/plugins/{plugin_name}/enable",
       "Enable a registered skill-hub plugin.", operations=(OP_UPDATE,)),
    _t("litellm_disable_plugin", _P, "POST",
       "/claude-code/plugins/{plugin_name}/disable",
       "Disable a registered skill-hub plugin.", operations=(OP_UPDATE,)),
    _t("litellm_skill_hub", _P, "GET", "/public/skill_hub",
       "Fetch the public skill-hub / marketplace catalog."),
)


TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_REGISTRY}


def categorized() -> dict[ToolCategory, list[ToolSpec]]:
    """Return the registry grouped by category (stable ordering)."""
    grouped: dict[ToolCategory, list[ToolSpec]] = {c: [] for c in ToolCategory}
    for spec in TOOL_REGISTRY:
        grouped[spec.category].append(spec)
    return grouped


def all_tool_names() -> list[str]:
    """Convenience: every registered tool name."""
    return [spec.name for spec in TOOL_REGISTRY]
