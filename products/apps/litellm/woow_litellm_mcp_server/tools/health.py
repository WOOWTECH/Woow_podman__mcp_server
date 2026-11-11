"""Health / readiness tools (category: health)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..deps import litellm_client
from ..gating import ToolGate
from ..registry import OP_CREATE
from ._common import prune_none, read_only, writing


def register(mcp: Any, gate: ToolGate) -> None:
    if gate.is_tool_enabled("litellm_health"):

        @mcp.tool(
            name="litellm_health",
            annotations=writing("Health"),
        )
        async def litellm_health(
            ctx: Context, model: str | None = None
        ) -> dict:
            """Active per-deployment health check; may incur provider charges.

            The backend may also save health history. Requires active-operation
            permission even when the backend serves cached health results.

            Optionally restrict the check to a single ``model``. Returns the
            healthy/unhealthy endpoint lists.

            An all-zero result for a ``model`` filter means *no deployment
            matched the name*, not "everything is fine" — a ``note`` is added to
            the payload in that case.
            """
            gate.require_operation("litellm_health", OP_CREATE)
            params = prune_none({"model": model})
            result = await litellm_client(ctx).get("/health", params=params)
            # LiteLLM answers an unknown model with the same zeroed envelope it
            # uses for "nothing to report", so `unhealthy_count: 0` reads as
            # healthy. Distinguish the two rather than letting a typo look green.
            if (
                model
                and isinstance(result, dict)
                and not result.get("healthy_endpoints")
                and not result.get("unhealthy_endpoints")
            ):
                result = {
                    **result,
                    "note": (
                        f"No deployment matched model={model!r}, so these zero "
                        f"counts do NOT mean the model is healthy. Call "
                        f"litellm_list_models for valid model_name values."
                    ),
                }
            return result

    if gate.is_tool_enabled("litellm_health_readiness"):

        @mcp.tool(
            name="litellm_health_readiness",
            annotations=read_only("Health readiness"),
        )
        async def litellm_health_readiness(ctx: Context) -> dict:
            """Gateway readiness including DB and cache status.

            This is the endpoint the admin console's connection-test probe hits.
            """
            return await litellm_client(ctx).get("/health/readiness")
