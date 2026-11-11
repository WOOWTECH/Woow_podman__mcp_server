"""路徑參數：'.'、'..'、空字串在送出前就拒絕，其餘編碼成單一路徑段。

key 原本原樣拼進路徑，httpx 組網址時又會把 '.'、'..' 段解掉：regenerate key '../../global/spend/reset?' 原本會用
master key 送出 POST /global/spend/reset。
"""
from __future__ import annotations

import pytest

from woow_litellm_mcp_server.errors import ToolError
from woow_litellm_mcp_server.tools import keys as keys_mod, models as models_mod, plugins as plugins_mod

from .conftest import FakeMCP, MockLiteLLM, build_gate, make_ctx

REFUSED = "must not be empty, '.' or '..'."


def _tool(module, name):
    mcp = FakeMCP()
    module.register(mcp, build_gate())
    return mcp.tools[name]


async def _call(gw: MockLiteLLM, tool, **kwargs):
    client = gw.client()
    try:
        return await tool(make_ctx(client), **kwargs)
    finally:
        await client.aclose()


@pytest.mark.parametrize("module,name,field", [
    (keys_mod, "litellm_regenerate_key", "key"),
    (plugins_mod, "litellm_enable_plugin", "plugin_name"),
    (plugins_mod, "litellm_delete_plugin", "plugin_name"),
    (models_mod, "litellm_update_model", "model_id"),
])
@pytest.mark.parametrize("value", ["", ".", ".."])
async def test_dot_segments_are_refused_before_any_request(module, name, field, value) -> None:
    gw = MockLiteLLM()
    with pytest.raises(ToolError, match=f"{field} {REFUSED}"):
        await _call(gw, _tool(module, name), **{field: value})
    assert gw.requests == []


async def test_a_spliced_path_stays_one_segment() -> None:
    gw = MockLiteLLM()
    await _call(gw, _tool(keys_mod, "litellm_regenerate_key"), key="../../global/spend/reset?")
    assert gw.last.method == "POST"
    assert gw.last.url.raw_path == b"/key/..%2F..%2Fglobal%2Fspend%2Freset%3F/regenerate"


async def test_update_model_keeps_the_id_in_one_segment() -> None:
    gw = MockLiteLLM()
    await _call(gw, _tool(models_mod, "litellm_update_model"), model_id="m/1", model_name="x")
    assert gw.last.method == "PATCH"
    assert gw.last.url.raw_path == b"/model/m%2F1/update"
