"""Claude-Code skill-hub plugin tools (category: plugins)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..deps import litellm_client
from ..gating import ToolGate
from ..registry import OP_CREATE, OP_DELETE, OP_UPDATE
from ._common import destructive, path_segment, prune_none, read_only, writing


def _plugin_path(plugin_name: str, suffix: str = "") -> str:
    """Build a plugin URL with the name percent-encoded.

    An unescaped f-string let a name containing ``#``, ``?`` or ``/`` be
    reinterpreted as URL structure: ``"p#x"`` dropped ``#x/enable`` as a
    fragment and the POST landed on ``/claude-code/plugins/p`` instead, giving a
    baffling 405 rather than a clean 404.
    """
    return f"/claude-code/plugins/{path_segment(plugin_name, 'plugin_name')}{suffix}"


def _coerce_source(source: dict[str, str] | str) -> dict[str, str]:
    """Normalise ``source`` into the object LiteLLM's schema requires.

    RegisterPluginRequest declares source as
    ``{"type": "object", "additionalProperties": {"type": "string"}}`` — e.g.
    ``{"source": "github", "repo": "org/repo"}``. A bare string is accepted for
    backwards compatibility (and because agents reach for one): ``"org/repo"``
    becomes the github form, anything else becomes ``{"source": value}``.
    """
    if isinstance(source, dict):
        return {str(k): str(v) for k, v in source.items()}
    value = str(source).strip()
    if (
        value
        and value.count("/") == 1
        and "://" not in value
        and not value.startswith("/")
        and " " not in value
    ):
        return {"source": "github", "repo": value}
    return {"source": value}


def register(mcp: Any, gate: ToolGate) -> None:
    if gate.is_tool_enabled("litellm_list_plugins"):

        @mcp.tool(
            name="litellm_list_plugins",
            annotations=read_only("List plugins"),
        )
        async def litellm_list_plugins(
            ctx: Context, enabled_only: bool = False
        ) -> Any:
            """List Claude-Code skill-hub plugins.

            Set ``enabled_only`` to hide disabled plugins.
            """
            params = prune_none(
                {"enabled_only": enabled_only if enabled_only else None}
            )
            return await litellm_client(ctx).get(
                "/claude-code/plugins", params=params
            )

    if gate.is_tool_enabled("litellm_plugin_info"):

        @mcp.tool(
            name="litellm_plugin_info",
            annotations=read_only("Plugin info"),
        )
        async def litellm_plugin_info(ctx: Context, plugin_name: str) -> Any:
            """Get one skill-hub plugin's record by name."""
            return await litellm_client(ctx).get(_plugin_path(plugin_name))

    if gate.is_tool_enabled("litellm_register_plugin"):

        @mcp.tool(
            name="litellm_register_plugin",
            annotations=writing("Register plugin"),
        )
        async def litellm_register_plugin(
            ctx: Context,
            name: str,
            source: dict[str, str] | str,
            version: str | None = None,
            description: str | None = None,
            category: str | None = None,
            domain: str | None = None,
            namespace: str | None = None,
            author: str | None = None,
            homepage: str | None = None,
            keywords: list[str] | None = None,
        ) -> dict:
            """Register a skill-hub plugin.

            ``source`` is an OBJECT describing where the plugin comes from, e.g.
            ``{"source": "github", "repo": "org/repo"}`` or
            ``{"source": "git-subdir", "url": ..., "path": ...}``. Declaring it
            as a plain string made every call fail upstream with a 422
            ``dict_type`` error; a bare ``"org/repo"`` string is still accepted
            and converted to the github form. ``name`` must match
            ``^[a-z0-9-]+$``.
            """
            gate.require_operation("litellm_register_plugin", OP_CREATE)
            body = prune_none(
                {
                    "name": name,
                    "source": _coerce_source(source),
                    "version": version,
                    "description": description,
                    "category": category,
                    "domain": domain,
                    "namespace": namespace,
                    "author": author,
                    "homepage": homepage,
                    "keywords": keywords,
                }
            )
            return await litellm_client(ctx).post(
                "/claude-code/plugins", json_data=body
            )

    if gate.is_tool_enabled("litellm_enable_plugin"):

        @mcp.tool(
            name="litellm_enable_plugin",
            annotations=writing("Enable plugin"),
        )
        async def litellm_enable_plugin(ctx: Context, plugin_name: str) -> dict:
            """Enable a registered skill-hub plugin."""
            gate.require_operation("litellm_enable_plugin", OP_UPDATE)
            return await litellm_client(ctx).post(
                _plugin_path(plugin_name, "/enable")
            )

    if gate.is_tool_enabled("litellm_disable_plugin"):

        @mcp.tool(
            name="litellm_disable_plugin",
            annotations=writing("Disable plugin"),
        )
        async def litellm_disable_plugin(ctx: Context, plugin_name: str) -> dict:
            """Disable a registered skill-hub plugin.

            The plugin keeps its name; use ``litellm_delete_plugin`` to free the
            name for re-registration under a different source.
            """
            gate.require_operation("litellm_disable_plugin", OP_UPDATE)
            return await litellm_client(ctx).post(
                _plugin_path(plugin_name, "/disable")
            )

    if gate.is_tool_enabled("litellm_delete_plugin"):

        @mcp.tool(
            name="litellm_delete_plugin",
            annotations=destructive("Delete plugin"),
        )
        async def litellm_delete_plugin(ctx: Context, plugin_name: str) -> Any:
            """[DESTRUCTIVE] Remove a registered skill-hub plugin by name.

            Unregisters the plugin and frees its name. Disabling is the
            reversible alternative (``litellm_disable_plugin``).
            """
            gate.require_operation("litellm_delete_plugin", OP_DELETE)
            return await litellm_client(ctx).delete(_plugin_path(plugin_name))

    if gate.is_tool_enabled("litellm_skill_hub"):

        @mcp.tool(
            name="litellm_skill_hub",
            annotations=read_only("Skill hub"),
        )
        async def litellm_skill_hub(ctx: Context) -> Any:
            """Fetch the public skill-hub / marketplace catalog of skills."""
            return await litellm_client(ctx).get("/public/skill_hub")
