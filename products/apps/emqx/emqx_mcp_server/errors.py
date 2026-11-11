"""EMQX API failures, translated into messages an LLM can act on."""

from __future__ import annotations

import httpx
from fastmcp.exceptions import ToolError


class EmqxApiError(ToolError):
    """Raised for any EMQX REST API failure.

    ToolError messages are always forwarded verbatim to the model, so they
    are written as instructions rather than stack traces.
    """

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


async def emqx_request(
    client, method: str, path: str, **kwargs
) -> httpx.Response:
    """Call the EMQX REST API, converting failures into actionable errors."""
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TimeoutException:
        raise EmqxApiError(
            "EMQX_TIMEOUT: the broker did not finish the request. Check state before repeating a write."
        ) from None
    except httpx.HTTPError as exc:
        from .http_policy import DestinationDenied, ResponseTooLarge
        raise EmqxApiError(
            "EMQX_DESTINATION_DENIED" if isinstance(exc, DestinationDenied) else
            "EMQX_RESPONSE_TOO_LARGE" if isinstance(exc, ResponseTooLarge) else
            "EMQX_UNAVAILABLE: the broker request failed. Check state before repeating a write."
        ) from None

    if response.status_code == 401:
        raise EmqxApiError(
            "EMQX rejected the API credentials (401). The configured API key "
            "or secret is wrong, expired, or disabled."
        )
    if response.status_code == 404:
        raise EmqxApiError(
            "EMQX has no such resource (404). "
            "Double-check the identifier — list the collection first.",
            status_code=404,
        )
    if 300 <= response.status_code < 400:
        raise EmqxApiError("EMQX_REDIRECT_DENIED: configure the final broker URL")
    if response.status_code >= 400:
        raise EmqxApiError(
            f"EMQX_API_ERROR status={response.status_code}"
        )
    return response


def json_body(response: httpx.Response) -> dict | list:
    """Parse a response body, tolerating EMQX's empty 204s."""
    if response.status_code == 204:
        return {}
    try:
        value = response.json()
        if not isinstance(value, (dict, list)):
            raise ValueError()
        return value
    except ValueError:
        raise EmqxApiError("EMQX_INVALID_RESPONSE: expected a JSON object or list") from None
