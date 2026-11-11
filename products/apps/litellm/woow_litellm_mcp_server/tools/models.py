"""Model-management tools (category: models)."""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..deps import litellm_client
from ..errors import ToolError
from ..gating import ToolGate
from ..registry import OP_CREATE, OP_DELETE, OP_UPDATE
from ._common import destructive, path_segment, prune_none, read_only, writing

#: Prefix LiteLLM uses in its *config file* to mean "read this from the
#: environment". The REST API does NOT expand it — see :func:`_reject_env_refs`.
_ENV_REF_PREFIX = "os.environ/"


def _reject_env_refs(litellm_params: dict[str, Any]) -> None:
    """Refuse ``os.environ/NAME`` values, which silently create a dead model.

    ``os.environ/OPENROUTER_API_KEY`` is valid in LiteLLM's *config.yaml*, where
    the loader expands it at startup. ``POST /model/new`` does no such thing: it
    encrypts the literal text ``os.environ/OPENROUTER_API_KEY`` and stores that
    as the credential. The call returns 200, the deployment shows up in
    ``/model/info``, and then every single completion through it fails with an
    upstream 401 forever. That is the worst possible failure shape — it looks
    like success — so it is rejected up front, naming the two fixes that
    actually work.
    """
    offenders = sorted(
        key
        for key, value in litellm_params.items()
        if isinstance(value, str) and value.startswith(_ENV_REF_PREFIX)
    )
    if not offenders:
        return
    raise ToolError(
        "litellm_params "
        + ", ".join(repr(k) for k in offenders)
        + " uses the 'os.environ/NAME' form, which only works in LiteLLM's "
        "config.yaml. The model-management REST API stores that text verbatim "
        "as the secret, so the model would be saved successfully and then fail "
        "every request with an upstream 401. Either omit the key entirely (LiteLLM "
        "falls back to the provider environment variable the gateway process "
        "already has, which is the usual answer for api_key) or pass the real "
        "secret value."
    )


def register(mcp: Any, gate: ToolGate) -> None:
    if gate.is_tool_enabled("litellm_list_models"):

        @mcp.tool(
            name="litellm_list_models",
            annotations=read_only("List models"),
        )
        async def litellm_list_models(ctx: Context) -> dict:
            """List all models registered in the LiteLLM gateway.

            Returns the OpenAI-style envelope ``{"data": [{"id": ...}]}``.
            """
            return await litellm_client(ctx).get("/v1/models")

    if gate.is_tool_enabled("litellm_model_info"):

        @mcp.tool(
            name="litellm_model_info",
            annotations=read_only("Model info"),
        )
        async def litellm_model_info(
            ctx: Context, litellm_model_id: str | None = None
        ) -> dict:
            """Full deployment detail per model.

            Provider, ``litellm_params`` and ``model_info`` for each deployment.
            Optionally filter to a single deployment by ``litellm_model_id``.
            """
            params = prune_none({"litellm_model_id": litellm_model_id})
            return await litellm_client(ctx).get("/model/info", params=params)

    if gate.is_tool_enabled("litellm_model_group_info"):

        @mcp.tool(
            name="litellm_model_group_info",
            annotations=read_only("Model group info"),
        )
        async def litellm_model_group_info(
            ctx: Context, model_group: str | None = None
        ) -> dict:
            """Aggregated per-model-group capabilities (context window, modes)."""
            params = prune_none({"model_group": model_group})
            return await litellm_client(ctx).get(
                "/model_group/info", params=params
            )

    if gate.is_tool_enabled("litellm_add_model"):

        @mcp.tool(
            name="litellm_add_model",
            annotations=writing("Add model"),
        )
        async def litellm_add_model(
            ctx: Context,
            model_name: str,
            litellm_params: dict[str, Any],
            model_info: dict[str, Any] | None = None,
        ) -> dict:
            """Register a new model deployment.

            ``litellm_params`` holds at least ``{"model": ..., "api_base":
            ...}``; ``model_info`` carries optional metadata.

            Do NOT pass ``api_key: "os.environ/SOMETHING"``. That indirection is
            a config.yaml feature and is not expanded by this API — it would be
            stored as the literal secret and the model would 401 on every
            request. Omit ``api_key`` to use the provider credential the gateway
            process already holds, or pass the real secret.
            """
            gate.require_operation("litellm_add_model", OP_CREATE)
            _reject_env_refs(litellm_params)
            body = prune_none(
                {
                    "model_name": model_name,
                    "litellm_params": litellm_params,
                    "model_info": model_info,
                }
            )
            return await litellm_client(ctx).post("/model/new", json_data=body)

    if gate.is_tool_enabled("litellm_update_model"):

        @mcp.tool(
            name="litellm_update_model",
            annotations=writing("Update model"),
        )
        async def litellm_update_model(
            ctx: Context,
            model_id: str,
            litellm_params: dict[str, Any] | None = None,
            model_info: dict[str, Any] | None = None,
            model_name: str | None = None,
        ) -> dict:
            """Update an existing deployment in place.

            ``model_id`` is the deployment id from ``litellm_add_model`` /
            ``litellm_model_info``. Every field is optional and independent:
            pass ``model_name`` to rename the deployment, ``model_info`` to
            change stored metadata, ``litellm_params`` to change routing or
            credentials. Whatever you omit is left exactly as it was — the
            fields you do pass are merged into the stored deployment rather
            than replacing it wholesale.

            The same ``os.environ/`` restriction as ``litellm_add_model``
            applies to any ``litellm_params`` value you pass.
            """
            gate.require_operation("litellm_update_model", OP_UPDATE)
            if litellm_params:
                _reject_env_refs(litellm_params)
            # Deliberately PATCH /model/{id}/update, NOT POST /model/update.
            # The latter is LiteLLM's legacy endpoint (its own docstring says
            # "Old endpoint for model update ... Use /model/{model_id}/update
            # to PATCH the stored model in db"): its handler writes only
            # litellm_params and updated_by to the database, so a rename or a
            # metadata change through it returns 200 and then silently does
            # nothing. The PATCH handler routes through update_db_model(),
            # which applies model_name, merges+re-encrypts litellm_params and
            # updates model_info, and does not demand a litellm_params body.
            merged_info = dict(model_info or {})
            merged_info["id"] = model_id
            body = prune_none(
                {
                    "model_name": model_name,
                    "model_info": merged_info,
                    "litellm_params": litellm_params,
                }
            )
            return await litellm_client(ctx).patch(
                f"/model/{path_segment(model_id, 'model_id')}/update", json_data=body
            )

    if gate.is_tool_enabled("litellm_delete_model"):

        @mcp.tool(
            name="litellm_delete_model",
            annotations=destructive("Delete model"),
        )
        async def litellm_delete_model(ctx: Context, model_id: str) -> dict:
            """[DESTRUCTIVE] Remove a model deployment by id.

            This unregisters the deployment from the gateway; it cannot be
            undone without re-adding the model.
            """
            gate.require_operation("litellm_delete_model", OP_DELETE)
            return await litellm_client(ctx).post(
                "/model/delete", json_data={"id": model_id}
            )
