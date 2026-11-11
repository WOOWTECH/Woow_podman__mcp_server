"""Every tool family builds the right HTTP request and parses the response.

One representative tool per category is exercised end-to-end through the real
tool code (register -> call), asserting the method, path, query params / JSON
body and the parsed return value. ``prune_none`` behaviour (never sending nulls)
is checked where it matters.
"""

from __future__ import annotations

import pytest

from woow_litellm_mcp_server.errors import ToolError
from woow_litellm_mcp_server.models import HealthStatus, KeyInfo, ModelList, PluginInfo
from woow_litellm_mcp_server.tools import (
    chat as chat_mod,
    health as health_mod,
    keys as keys_mod,
    models as models_mod,
    plugins as plugins_mod,
    teams as teams_mod,
    users as users_mod,
)

from .conftest import FakeMCP, MockLiteLLM, build_gate, make_ctx


def _register(module) -> dict:
    mcp = FakeMCP()
    module.register(mcp, build_gate())
    return mcp.tools


async def _call(gw: MockLiteLLM, tool, **kwargs):
    client = gw.client()
    try:
        return await tool(make_ctx(client), **kwargs)
    finally:
        await client.aclose()


# --- models ---------------------------------------------------------------
async def test_list_models_get_and_parse() -> None:
    gw = MockLiteLLM().route("/v1/models", {"object": "list", "data": [{"id": "gpt-4o"}]})
    result = await _call(gw, _register(models_mod)["litellm_list_models"])
    assert gw.last.method == "GET"
    assert gw.last.url.path == "/v1/models"
    parsed = ModelList.model_validate(result)
    assert parsed.data[0].id == "gpt-4o"


async def test_add_model_posts_body() -> None:
    gw = MockLiteLLM().route("/model/new", {"model_id": "m-1"})
    tool = _register(models_mod)["litellm_add_model"]
    result = await _call(
        gw,
        tool,
        model_name="my-gpt",
        litellm_params={"model": "openai/gpt-4o", "api_key": "sk-x"},
    )
    assert gw.last.method == "POST"
    assert gw.last.url.path == "/model/new"
    body = gw.last_json()
    assert body["model_name"] == "my-gpt"
    assert body["litellm_params"]["model"] == "openai/gpt-4o"
    assert "model_info" not in body  # None was pruned
    assert result == {"model_id": "m-1"}


async def test_delete_model_sends_id() -> None:
    gw = MockLiteLLM().route("/model/delete", {"deleted": True})
    tool = _register(models_mod)["litellm_delete_model"]
    await _call(gw, tool, model_id="m-9")
    assert gw.last.url.path == "/model/delete"
    assert gw.last_json() == {"id": "m-9"}


# --- keys -----------------------------------------------------------------
async def test_generate_key_prunes_none() -> None:
    gw = MockLiteLLM().route("/key/generate", {"key": "sk-new"})
    tool = _register(keys_mod)["litellm_generate_key"]
    result = await _call(gw, tool, key_alias="ci", max_budget=10.0)
    assert gw.last.method == "POST"
    assert gw.last.url.path == "/key/generate"
    body = gw.last_json()
    assert body == {"key_alias": "ci", "max_budget": 10.0}  # everything else pruned
    assert result["key"] == "sk-new"


async def test_key_info_get_with_param_and_model() -> None:
    payload = {"token": "sk-abc", "key_alias": "ci", "spend": 1.25, "max_budget": 10.0}
    gw = MockLiteLLM().route("/key/info", payload)
    tool = _register(keys_mod)["litellm_key_info"]
    result = await _call(gw, tool, key="sk-abc")
    assert gw.last.url.path == "/key/info"
    assert gw.last_params()["key"] == "sk-abc"
    parsed = KeyInfo.model_validate(result)
    assert parsed.spend == 1.25


async def test_regenerate_key_uses_path_segment() -> None:
    gw = MockLiteLLM().route("/key/sk-abc/regenerate", {"key": "sk-rotated"})
    tool = _register(keys_mod)["litellm_regenerate_key"]
    result = await _call(gw, tool, key="sk-abc")
    assert gw.last.method == "POST"
    assert gw.last.url.path == "/key/sk-abc/regenerate"
    assert result["key"] == "sk-rotated"


# --- health ---------------------------------------------------------------
async def test_health_readiness_get_and_parse() -> None:
    payload = {"status": "connected", "db": "connected", "healthy_count": 3}
    gw = MockLiteLLM().route("/health/readiness", payload)
    tool = _register(health_mod)["litellm_health_readiness"]
    result = await _call(gw, tool)
    assert gw.last.url.path == "/health/readiness"
    parsed = HealthStatus.model_validate(result)
    assert parsed.status == "connected"
    assert parsed.db == "connected"


# --- plugins (Claude-Code skill hub) --------------------------------------
async def test_list_plugins_omits_false_enabled_only() -> None:
    gw = MockLiteLLM().route("/claude-code/plugins", [{"name": "p", "enabled": True}])
    tool = _register(plugins_mod)["litellm_list_plugins"]
    result = await _call(gw, tool)  # enabled_only defaults False -> pruned
    assert gw.last.url.path == "/claude-code/plugins"
    assert "enabled_only" not in gw.last_params()
    parsed = [PluginInfo.model_validate(p) for p in result]
    assert parsed[0].name == "p"


async def test_enable_plugin_posts_to_named_path() -> None:
    gw = MockLiteLLM().route("/claude-code/plugins/my-plugin/enable", {"enabled": True})
    tool = _register(plugins_mod)["litellm_enable_plugin"]
    result = await _call(gw, tool, plugin_name="my-plugin")
    assert gw.last.method == "POST"
    assert gw.last.url.path == "/claude-code/plugins/my-plugin/enable"
    assert result == {"enabled": True}


async def test_update_model_patches_the_id_scoped_path() -> None:
    """The deployment is addressed by URL, and model_info still carries the id.

    LiteLLM's PATCH handler resolves the row from the path segment, but the
    stored ``model_info`` blob is merged rather than replaced, so echoing the
    id back is free and keeps the two representations agreeing.
    """
    gw = MockLiteLLM().route("/model/m-1/update", {"model_id": "m-1"})
    tool = _register(models_mod)["litellm_update_model"]
    await _call(
        gw,
        tool,
        model_id="m-1",
        litellm_params={"model": "openai/gpt-4o"},
        model_info={"mode": "chat"},
    )
    assert gw.last.method == "PATCH"
    assert gw.last.url.path == "/model/m-1/update"
    body = gw.last_json()
    assert body["model_info"]["id"] == "m-1"
    assert body["model_info"]["mode"] == "chat"
    assert "model_id" not in body


async def test_delete_key_refuses_an_empty_selector() -> None:
    """No selectors means an empty body to a BULK delete endpoint."""
    gw = MockLiteLLM().route("/key/delete", {"deleted": 0})
    tool = _register(keys_mod)["litellm_delete_key"]
    with pytest.raises(ToolError):
        await _call(gw, tool)
    assert not gw.requests, "nothing may reach the gateway"


async def test_update_key_clear_survives_prune_none() -> None:
    """`clear` is the only way to reset a field; nulls must not be pruned."""
    gw = MockLiteLLM().route("/key/update", {"key": "sk-abc"})
    tool = _register(keys_mod)["litellm_update_key"]
    await _call(gw, tool, key="sk-abc", key_alias="ci", clear=["max_budget"])
    body = gw.last_json()
    assert body["key_alias"] == "ci"
    assert "max_budget" in body and body["max_budget"] is None

    with pytest.raises(ToolError):
        await _call(gw, tool, key="sk-abc", clear=["not_a_field"])


async def test_delete_user_wraps_the_bare_integer_response() -> None:
    """/user/delete answers `1`, which failed the tool's `-> dict` schema.

    The deletion had already happened, so the caller was told it failed while
    the users and their keys were gone.
    """
    gw = MockLiteLLM().route("/user/delete", 1)
    tool = _register(users_mod)["litellm_delete_user"]
    result = await _call(gw, tool, user_ids=["u-1"])
    assert isinstance(result, dict)
    assert result == {"deleted": 1, "user_ids": ["u-1"]}


async def test_update_user_does_not_send_a_teams_field() -> None:
    """UpdateUserRequest has no `teams`; pydantic dropped it silently."""
    gw = MockLiteLLM().route("/user/update", {"user_id": "u-1"})
    tool = _register(users_mod)["litellm_update_user"]
    await _call(gw, tool, user_id="u-1", user_alias="Ada", team_id="t-1")
    body = gw.last_json()
    assert "teams" not in body
    assert body["user_alias"] == "Ada"
    assert body["team_id"] == "t-1"


async def test_list_teams_sends_page_size_not_size() -> None:
    """/v2/team/list ignores `size` and silently caps results at 10."""
    gw = MockLiteLLM().route("/v2/team/list", {"teams": []})
    tool = _register(teams_mod)["litellm_list_teams"]
    await _call(gw, tool, page=2, page_size=100)
    params = gw.last_params()
    assert params["page_size"] == "100"
    assert "size" not in params


async def test_health_flags_an_unmatched_model_filter() -> None:
    """All-zero counts for an unknown model read as "everything is fine"."""
    gw = MockLiteLLM().route(
        "/health", {"healthy_endpoints": [], "unhealthy_endpoints": [],
                    "healthy_count": 0, "unhealthy_count": 0}
    )
    tool = _register(health_mod)["litellm_health"]
    result = await _call(gw, tool, model="typo-model")
    assert "typo-model" in result["note"]


# --- chat -----------------------------------------------------------------
async def test_extra_body_cannot_override_the_explicit_model() -> None:
    """extra_body used to be merged last, redirecting the call silently."""
    gw = MockLiteLLM().route("/v1/chat/completions", {"id": "c-1"})
    tool = _register(chat_mod)["litellm_chat_completion"]
    await _call(
        gw,
        tool,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"model": "expensive-model", "seed": 7},
    )
    body = gw.last_json()
    assert body["model"] == "gpt-4o-mini"
    assert body["seed"] == 7  # non-conflicting keys still pass through


async def test_streaming_sse_is_reassembled_into_a_chat_completion() -> None:
    """stream=true returned a raw SSE blob, contradicting the docstring."""
    sse = (
        'data: {"id":"c-1","model":"gpt-4o-mini","choices":'
        '[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n'
        'data: {"id":"c-1","choices":[{"index":0,"delta":{"content":"lo"},'
        '"finish_reason":"stop"}],"usage":{"total_tokens":5}}\n\n'
        "data: [DONE]\n\n"
    )
    gw = MockLiteLLM().default({"raw": sse})
    tool = _register(chat_mod)["litellm_chat_completion"]
    result = await _call(
        gw,
        tool,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "Hello"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 5
    assert "raw" not in result


# --- plugins --------------------------------------------------------------
async def test_register_plugin_sends_source_as_an_object() -> None:
    """RegisterPluginRequest.source is an object; a str 422s upstream."""
    gw = MockLiteLLM().route("/claude-code/plugins", {"name": "p"})
    tool = _register(plugins_mod)["litellm_register_plugin"]
    await _call(gw, tool, name="p", source={"source": "github", "repo": "org/repo"})
    assert gw.last_json()["source"] == {"source": "github", "repo": "org/repo"}

    # A bare "org/repo" string stays usable and is converted, not passed on.
    await _call(gw, tool, name="p", source="org/repo")
    assert gw.last_json()["source"] == {"source": "github", "repo": "org/repo"}


async def test_plugin_name_is_percent_encoded_in_the_path() -> None:
    """An unescaped '#' turned the rest of the path into a URL fragment."""
    gw = MockLiteLLM().default({"ok": True})
    tool = _register(plugins_mod)["litellm_enable_plugin"]
    await _call(gw, tool, plugin_name="my-plugin#x")
    # raw_path is what went on the wire; httpx's .path decodes it back again.
    assert gw.last.url.raw_path == b"/claude-code/plugins/my-plugin%23x/enable"


async def test_delete_and_read_single_plugin_exist() -> None:
    """Without a delete tool an MCP-registered plugin was unremovable."""
    gw = MockLiteLLM().default({"name": "p", "enabled": True})
    tools = _register(plugins_mod)
    await _call(gw, tools["litellm_plugin_info"], plugin_name="p")
    assert gw.last.method == "GET"
    assert gw.last.url.path == "/claude-code/plugins/p"

    await _call(gw, tools["litellm_delete_plugin"], plugin_name="p")
    assert gw.last.method == "DELETE"
    assert gw.last.url.path == "/claude-code/plugins/p"


def test_plugin_info_model_accepts_an_object_source() -> None:
    """PluginInfo.source was `str`, so every real payload failed validation."""
    parsed = PluginInfo.model_validate(
        {
            "id": "pl-1",
            "name": "my-plugin",
            "source": {"source": "github", "repo": "org/repo"},
            "enabled": True,
            "created_at": "2026-08-01T00:00:00Z",
        }
    )
    assert parsed.source == {"source": "github", "repo": "org/repo"}
    # A legacy bare string is coerced rather than rejected.
    assert PluginInfo.model_validate({"name": "p", "source": "org/repo"}).source == {
        "source": "org/repo"
    }
