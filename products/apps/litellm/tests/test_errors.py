"""Error mapping in ``errors.litellm_request`` — actionable ToolError messages."""

from __future__ import annotations

import httpx
import pytest

from woow_litellm_mcp_server.errors import (
    LiteLLMApiError,
    json_body,
    litellm_request,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://litellm.test",
        transport=httpx.MockTransport(handler),
    )


async def test_401_names_the_master_key() -> None:
    client = _client(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    try:
        with pytest.raises(LiteLLMApiError) as exc:
            await litellm_request(client, "GET", "/key/list")
        assert "MASTER_KEY" in str(exc.value)
        assert "bad key" in str(exc.value)
    finally:
        await client.aclose()


async def test_upstream_provider_401_does_not_blame_the_master_key() -> None:
    """A 401 from the model's provider is not a 401 from the proxy.

    LiteLLM returns the same status for both, but the fixes are opposites: one
    is the operator's LITELLM_MCP_MASTER_KEY, the other is that deployment's
    api_key. Reporting the provider's rejection as "the master key is wrong"
    sends an operator to rotate a working key — and rotating the master key is
    an outage.
    """
    body = (
        "litellm.AuthenticationError: AuthenticationError: "
        "OpenrouterException - No auth credentials found"
    )
    client = _client(lambda r: httpx.Response(401, json={"error": {"message": body}}))
    try:
        with pytest.raises(LiteLLMApiError) as exc:
            await litellm_request(
                client, "POST", "/chat/completions", json_data={}
            )
        message = str(exc.value)
        assert "LITELLM_MCP_MASTER_KEY" not in message
        assert "do not" in message.lower() and "rotate" in message.lower()
        assert "api_key" in message
        assert "OpenrouterException" in message
    finally:
        await client.aclose()


async def test_proxy_401_still_names_the_master_key() -> None:
    """The proxy's own rejection wins even when auth words are in the body."""
    client = _client(
        lambda r: httpx.Response(
            401,
            json={
                "error": {
                    "message": "Authentication Error, Invalid proxy server "
                    "token passed. AuthenticationError: key not found"
                }
            },
        )
    )
    try:
        with pytest.raises(LiteLLMApiError) as exc:
            await litellm_request(client, "GET", "/key/list")
        assert "LITELLM_MCP_MASTER_KEY" in str(exc.value)
    finally:
        await client.aclose()


async def test_enterprise_gate_is_reported_as_unavailable_not_as_a_fault() -> None:
    """Community edition answers Enterprise endpoints with a 500 + sales copy.

    Raw, that reads as "the gateway is broken, retry" — so a caller retries a
    call that can never succeed. It has to say: not your request, not a fault,
    do not retry.
    """
    body = (
        "Regenerating Virtual Keys is an Enterprise feature, You must be a "
        "LiteLLM Enterprise user to use this feature. If you have a license "
        "please set `LITELLM_LICENSE` in your env."
    )
    client = _client(lambda r: httpx.Response(500, json={"error": {"message": body}}))
    try:
        with pytest.raises(LiteLLMApiError) as exc:
            await litellm_request(client, "POST", "/key/sk-x/regenerate")
        message = str(exc.value)
        assert "Enterprise" in message
        assert "community edition" in message
        assert "retrying will not help" in message
        assert "LITELLM_LICENSE" in message
    finally:
        await client.aclose()


async def test_a_plain_500_is_not_mistaken_for_the_enterprise_gate() -> None:
    client = _client(lambda r: httpx.Response(500, json={"error": {"message": "boom"}}))
    try:
        with pytest.raises(LiteLLMApiError) as exc:
            await litellm_request(client, "GET", "/health")
        message = str(exc.value)
        assert "Enterprise" not in message
        assert "boom" in message
    finally:
        await client.aclose()


async def test_404_names_the_endpoint() -> None:
    client = _client(lambda r: httpx.Response(404, json={"detail": "nope"}))
    try:
        with pytest.raises(LiteLLMApiError) as exc:
            await litellm_request(client, "GET", "/does/not/exist")
        assert "404" in str(exc.value)
        assert "/does/not/exist" in str(exc.value)
    finally:
        await client.aclose()


async def test_422_surfaces_validation_body() -> None:
    client = _client(lambda r: httpx.Response(422, json={"detail": "field required"}))
    try:
        with pytest.raises(LiteLLMApiError) as exc:
            await litellm_request(client, "POST", "/key/generate", json_data={})
        assert "field required" in str(exc.value)
    finally:
        await client.aclose()


async def test_connect_error_mentions_port_4000() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(boom)
    try:
        with pytest.raises(LiteLLMApiError) as exc:
            await litellm_request(client, "GET", "/v1/models")
        assert "4000" in str(exc.value)
    finally:
        await client.aclose()


async def test_params_with_none_are_dropped() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    client = _client(handler)
    try:
        await litellm_request(
            client, "GET", "/key/list", params={"page": 1, "user_id": None}
        )
        assert seen["params"] == {"page": "1"}
    finally:
        await client.aclose()


def test_json_body_tolerates_empty_and_204() -> None:
    assert json_body(httpx.Response(204)) == {}
    assert json_body(httpx.Response(200, content=b"")) == {}
    assert json_body(httpx.Response(200, json={"a": 1})) == {"a": 1}
    # Non-JSON body is wrapped rather than raising.
    assert json_body(httpx.Response(200, content=b"not json")) == {"raw": "not json"}
