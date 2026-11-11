"""The contracts LiteLLM's model-write endpoints impose, pinned as tests.

All were found by driving the deployed server against a live LiteLLM gateway,
and each fails in a way that does not look like the tool's fault:

* ``POST /model/update`` is LiteLLM's *legacy* endpoint. Its handler writes
  only ``litellm_params`` and ``updated_by`` to the database, so a rename or a
  ``model_info`` change through it answers 200 and then silently does nothing;
  it also rejects a body with no ``litellm_params`` at all, with a bare
  "litellm_params not provided" wrapped in a 400. LiteLLM's own docstring says
  to use ``PATCH /model/{model_id}/update`` instead, and that handler applies
  ``model_name``, merges ``litellm_params`` and updates ``model_info``. The
  tool therefore PATCHes the id-scoped path — see the tests below.
* ``/model/new`` accepts ``api_key: "os.environ/NAME"``, encrypts the literal
  text, answers 200, and produces a deployment that 401s on every request
  forever.

The last is the dangerous one: it is shaped like success. Nothing downstream
can tell you the model is dead until a user's completion fails, and the 401 it
then produces points at the wrong credential entirely (see
``tests/test_errors.py``). Hence a pre-flight refusal rather than a doc note.
"""

from __future__ import annotations

import pytest

from woow_litellm_mcp_server.errors import ToolError
from woow_litellm_mcp_server.registry import TOOLS_BY_NAME
from woow_litellm_mcp_server.tools import models as models_mod

from .conftest import FakeMCP, MockLiteLLM, build_gate, make_ctx


def _tool(name: str):
    mcp = FakeMCP()
    models_mod.register(mcp, build_gate())
    return mcp.tools[name]


async def _call(gw: MockLiteLLM, tool, **kwargs):
    client = gw.client()
    try:
        return await tool(make_ctx(client), **kwargs)
    finally:
        await client.aclose()


async def test_update_model_patches_the_id_scoped_endpoint() -> None:
    """A rename must go to PATCH /model/{id}/update, never POST /model/update.

    This is the whole bug: the legacy POST handler persists only
    ``litellm_params``, so it returned 200 while ``/model/info`` kept reporting
    the old ``model_name`` forever. Pin the method and the path shape.
    """
    gw = MockLiteLLM().route("/model/m-1/update", {"model_id": "m-1"})
    await _call(gw, _tool("litellm_update_model"), model_id="m-1", model_name="renamed")

    assert gw.last.method == "PATCH"
    assert gw.last.url.path == "/model/m-1/update"
    assert gw.last_json()["model_name"] == "renamed"
    # 管理面 registry 必須描述 handler 實際送出的請求。
    spec = TOOLS_BY_NAME["litellm_update_model"]
    assert spec.method == gw.last.method
    assert spec.path.format(model_id="m-1") == gw.last.url.path


async def test_update_model_omits_litellm_params_when_not_given() -> None:
    """PATCH does not demand litellm_params, so a rename must not invent one.

    Sending ``{}`` here would be harmless but dishonest; sending nothing keeps
    the request a true partial update of exactly the fields the caller named.
    """
    gw = MockLiteLLM().route("/model/m-1/update", {"model_id": "m-1"})
    await _call(gw, _tool("litellm_update_model"), model_id="m-1", model_name="renamed")

    body = gw.last_json()
    assert "litellm_params" not in body, body
    assert body["model_info"]["id"] == "m-1"


async def test_update_model_passes_real_params_through_untouched() -> None:
    """Params the caller did supply must reach the gateway verbatim."""
    gw = MockLiteLLM().route("/model/m-1/update", {"model_id": "m-1"})
    await _call(
        gw,
        _tool("litellm_update_model"),
        model_id="m-1",
        litellm_params={"model": "openai/gpt-4o", "rpm": 60},
    )
    assert gw.last_json()["litellm_params"] == {"model": "openai/gpt-4o", "rpm": 60}


async def test_update_model_sends_model_info_metadata() -> None:
    """PATCH *does* persist model_info, so metadata keys must be forwarded."""
    gw = MockLiteLLM().route("/model/m-1/update", {"model_id": "m-1"})
    await _call(
        gw,
        _tool("litellm_update_model"),
        model_id="m-1",
        model_info={"description": "notes"},
    )
    info = gw.last_json()["model_info"]
    assert info["description"] == "notes"
    assert info["id"] == "m-1"


async def test_update_model_escapes_the_id_into_one_path_segment() -> None:
    """A model_id is now part of the URL — it must not be able to traverse it."""
    gw = MockLiteLLM().default({"model_id": "x"})
    await _call(
        gw,
        _tool("litellm_update_model"),
        model_id="../../key/delete",
        model_name="renamed",
    )
    # ``.path`` un-quotes; ``.raw_path`` is what actually goes out on the wire.
    assert gw.last.url.raw_path == b"/model/..%2F..%2Fkey%2Fdelete/update"


@pytest.mark.parametrize("tool_name", ["litellm_add_model", "litellm_update_model"])
async def test_env_ref_api_key_is_refused_before_the_request(tool_name: str) -> None:
    """``os.environ/NAME`` never reaches the gateway, on either endpoint."""
    gw = MockLiteLLM().default({"model_id": "m-1"})
    extra = (
        {"model_name": "m"}
        if tool_name == "litellm_add_model"
        else {"model_id": "m-1"}
    )

    with pytest.raises(ToolError) as exc:
        await _call(
            gw,
            _tool(tool_name),
            litellm_params={
                "model": "openrouter/openai/gpt-4o-mini",
                "api_key": "os.environ/OPENROUTER_API_KEY",
            },
            **extra,
        )

    message = str(exc.value)
    assert "api_key" in message
    assert "os.environ/" in message
    assert "omit" in message.lower(), "it must name the fix, not just say no"
    assert not gw.requests, "the dead model must never reach the gateway"


async def test_every_offending_key_is_named_not_just_the_first() -> None:
    """An operator fixing one key at a time round-trips for each one."""
    gw = MockLiteLLM().default({})
    with pytest.raises(ToolError) as exc:
        await _call(
            gw,
            _tool("litellm_add_model"),
            model_name="m",
            litellm_params={
                "model": "openrouter/openai/gpt-4o-mini",
                "api_key": "os.environ/OPENROUTER_API_KEY",
                "api_base": "os.environ/OPENROUTER_BASE",
            },
        )
    message = str(exc.value)
    assert "api_key" in message and "api_base" in message


async def test_a_real_api_key_still_goes_through() -> None:
    """The guard keys off the os.environ/ prefix only, not the word api_key."""
    gw = MockLiteLLM().route("/model/new", {"model_id": "m-1"})
    await _call(
        gw,
        _tool("litellm_add_model"),
        model_name="m",
        litellm_params={"model": "openai/gpt-4o", "api_key": "sk-real"},
    )
    assert gw.last_json()["litellm_params"]["api_key"] == "sk-real"


async def test_non_string_params_do_not_trip_the_guard() -> None:
    """rpm/tpm ints and nested dicts must not raise inside the prefix check."""
    gw = MockLiteLLM().route("/model/new", {"model_id": "m-1"})
    await _call(
        gw,
        _tool("litellm_add_model"),
        model_name="m",
        litellm_params={
            "model": "openai/gpt-4o",
            "rpm": 100,
            "tpm": None,
            "extra_headers": {"x-a": "b"},
        },
    )
    assert gw.last_json()["litellm_params"]["rpm"] == 100
