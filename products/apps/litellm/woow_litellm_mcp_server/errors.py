"""Error handling and the low-level LiteLLM request helper.

``litellm_request`` performs an HTTP call against the pooled client and maps the
common failure modes into ``ToolError`` messages that are *actionable for an
LLM*: it names the likely cause (upstream down on port 4000, wrong master key,
not found, validation error) and surfaces the LiteLLM error body verbatim.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

try:  # FastMCP ships ToolError; fall back to a plain exception if absent.
    from fastmcp.exceptions import ToolError
except Exception:  # pragma: no cover - defensive import

    class ToolError(Exception):  # type: ignore[no-redef]
        """Fallback ToolError when fastmcp is unavailable."""


class LiteLLMApiError(ToolError):
    """Raised when the LiteLLM gateway returns an error or is unreachable."""


def _extract_error_body(response: httpx.Response) -> str:
    """Pull a human/LLM-readable error message out of a LiteLLM response."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        text = (response.text or "").strip()
        return text or f"HTTP {response.status_code}"

    # LiteLLM/OpenAI error envelope: {"error": {"message": ...}} or {"detail": ...}
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        if payload.get("detail") is not None:
            detail = payload["detail"]
            return detail if isinstance(detail, str) else json.dumps(detail)
        if payload.get("message"):
            return str(payload["message"])
    return json.dumps(payload)


# A 401 out of LiteLLM has two completely different causes that need two
# completely different fixes, and the body is the only thing that tells them
# apart. Either OUR master key was rejected by the proxy (fix: the operator's
# LITELLM_MCP_MASTER_KEY), or the proxy authenticated us fine and the model's
# *upstream provider* rejected LiteLLM's credential (fix: that deployment's
# api_key — nothing to do with us). Blaming the master key for the second case
# sends an operator to rotate a key that was never wrong.
_PROVIDER_AUTH_MARKERS = (
    "litellm.authenticationerror",
    "authenticationerror:",
    "invalid api key",
    "incorrect api key",
    "no auth credentials found",
)
#: LiteLLM names upstream failures ``OpenrouterException``, ``OpenAIException``,
#: ``AnthropicException``… — a reliable "this came from the provider" tell.
_PROVIDER_EXCEPTION_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*Exception\b")

#: …but the proxy's own rejection wins if both are somehow present, because
#: these strings only ever come from LiteLLM's own key-auth layer.
_PROXY_AUTH_MARKERS = (
    "invalid proxy server token",
    "invalid user key",
    "no api key passed",
    "no api key provided",
)

#: The community edition answers Enterprise-only endpoints with a wall of
#: marketing copy. Retrying cannot help; the caller needs to know that.
_ENTERPRISE_MARKERS = (
    "enterprise feature",
    "litellm_license",
    "litellm enterprise",
    "litellm.ai/enterprise",
)


def _is_provider_auth_failure(body: str) -> bool:
    lowered = body.lower()
    if any(marker in lowered for marker in _PROXY_AUTH_MARKERS):
        return False
    if any(marker in lowered for marker in _PROVIDER_AUTH_MARKERS):
        return True
    return bool(_PROVIDER_EXCEPTION_RE.search(body))


def _is_enterprise_gate(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _ENTERPRISE_MARKERS)


def json_body(response: httpx.Response) -> Any:
    """Return the JSON body, tolerating 204/empty responses."""
    if response.status_code == 204 or not (response.content or b"").strip():
        return {}
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        return {"raw": response.text}


async def litellm_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_data: Any | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request, mapping transport/HTTP errors to ToolError.

    Returns the raw ``httpx.Response`` on success (2xx); callers use
    :func:`json_body` to decode it.
    """
    # Drop query params that are None so we don't send ?x=None to LiteLLM.
    if params:
        params = {k: v for k, v in params.items() if v is not None}

    try:
        response = await client.request(
            method.upper(),
            path,
            params=params,
            json=json_data,
            **kwargs,
        )
    except httpx.ConnectError as exc:
        raise LiteLLMApiError(
            f"Cannot reach the LiteLLM gateway at {client.base_url!s} "
            f"(connection refused). Is LiteLLM running and listening on "
            f"port 4000? Underlying error: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise LiteLLMApiError(
            f"Request to LiteLLM timed out ({method.upper()} {path}). "
            f"The gateway may be overloaded or the operation too slow. "
            f"Underlying error: {exc}"
        ) from exc
    except httpx.HTTPError as exc:  # pragma: no cover - catch-all transport error
        raise LiteLLMApiError(
            f"HTTP error talking to LiteLLM ({method.upper()} {path}): {exc}"
        ) from exc

    if response.is_success:
        return response

    body = _extract_error_body(response)
    status = response.status_code
    # Checked before the per-status branches: the enterprise gate answers with
    # whatever status it likes (500 for /key/*/regenerate) and the status alone
    # would send the caller off to debug a server fault that does not exist.
    if _is_enterprise_gate(body):
        raise LiteLLMApiError(
            f"{method.upper()} {path} is a LiteLLM Enterprise-only feature and "
            f"this gateway runs the community edition, so it answered {status}. "
            f"Nothing about the request was wrong and retrying will not help — "
            f"use a different approach (for example, delete and re-create a key "
            f"instead of regenerating it) or have the operator set "
            f"LITELLM_LICENSE. Details: {body}"
        )
    if status == 401:
        if _is_provider_auth_failure(body):
            raise LiteLLMApiError(
                f"The LiteLLM gateway accepted this request but the model's "
                f"upstream provider rejected LiteLLM's own credential (401). "
                f"The MCP server's master key is NOT the problem — do not "
                f"rotate it. Check that deployment's api_key: a model added via "
                f"litellm_add_model with an 'os.environ/NAME' api_key stores "
                f"that text literally and 401s forever, so re-add it with the "
                f"key omitted or with the real secret. Details: {body}"
            )
        raise LiteLLMApiError(
            "LiteLLM rejected the credentials (401 Unauthorized). The "
            "LITELLM_MCP_MASTER_KEY is missing or wrong. Details: " + body
        )
    if status == 403:
        raise LiteLLMApiError(
            "LiteLLM forbade the request (403). The key lacks admin scope "
            "for this operation. Details: " + body
        )
    if status == 404:
        # Lead with the resource interpretation: in practice ~every 404 here is
        # a bad identifier ("Key not found in database"), and telling an LLM the
        # *endpoint* may not exist sends it hunting for a different route or
        # concluding the server is misconfigured.
        raise LiteLLMApiError(
            f"LiteLLM returned 404 Not Found for {method.upper()} {path} — the "
            f"requested resource does not exist; check the identifier you "
            f"passed. (If the identifier is definitely correct, the endpoint "
            f"itself may be unavailable on this gateway.) Details: {body}"
        )
    if status == 422 or status == 400:
        raise LiteLLMApiError(
            f"LiteLLM rejected the request body ({status}). Check the "
            f"parameters. Details: {body}"
        )
    raise LiteLLMApiError(
        f"LiteLLM returned an error ({status}) for {method.upper()} {path}: {body}"
    )
