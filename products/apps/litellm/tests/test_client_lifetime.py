"""The borrowed-handle contract from ``deps.py``.

The load-bearing gotcha: tools receive a *borrowed* handle to the pooled client
owned by the lifespan. They must never close it — doing so would break every
later tool call in the same process. These tests pin that behaviour.
"""

from __future__ import annotations

import pytest

from woow_litellm_mcp_server.deps import LiteLLMHttp, litellm_client
from woow_litellm_mcp_server.errors import ToolError

from .conftest import MockLiteLLM, make_ctx


def test_handle_has_no_close_method() -> None:
    # The borrowed handle deliberately exposes no aclose()/close().
    assert not hasattr(LiteLLMHttp, "aclose")
    assert not hasattr(LiteLLMHttp, "close")


async def test_handle_stays_usable_across_multiple_tool_calls() -> None:
    gw = MockLiteLLM().default({"ok": True})
    client = gw.client()
    try:
        ctx = make_ctx(client)

        # Each tool call obtains its own borrowed handle from the same pool.
        first = litellm_client(ctx)
        assert await first.get("/health/readiness") == {"ok": True}
        assert not client.is_closed

        second = litellm_client(ctx)
        assert await second.get("/v1/models") == {"ok": True}
        # The underlying pooled client is still open after several calls.
        assert not client.is_closed
        assert len(gw.requests) == 2
    finally:
        await client.aclose()


async def test_missing_lifespan_context_raises_toolerror() -> None:
    from types import SimpleNamespace

    empty = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=None))
    with pytest.raises(ToolError):
        litellm_client(empty)


async def test_closed_client_raises_toolerror() -> None:
    gw = MockLiteLLM()
    client = gw.client()
    await client.aclose()
    ctx = make_ctx(client)
    with pytest.raises(ToolError):
        litellm_client(ctx)


async def test_missing_client_key_raises_toolerror() -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context={}))
    with pytest.raises(ToolError):
        litellm_client(ctx)
