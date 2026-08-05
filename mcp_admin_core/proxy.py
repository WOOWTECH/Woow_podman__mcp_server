"""MCP reverse proxy — replaces the nginx auth proxy.

Routes ``/private_{token}/{path}`` requests to the MCP server on
localhost, validating the token against the config store.  Supports
streaming responses required by the MCP protocol (SSE, chunked).

This is where Claude's MCP connector actually reaches ``tools/list``
and ``tools/call`` on the Podman MCP server.  Because FastMCP speaks
the stateful Streamable-HTTP flow, the ``Mcp-Session-Id`` and
``MCP-Protocol-Version`` headers are relayed transparently in both
directions and both ``application/json`` and ``text/event-stream``
responses pass through untouched — a plain pass-through stream already
satisfies this.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .config import get_config_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-proxy"])

# Shared client with long timeout for MCP streaming calls.  The cache is keyed
# on the configured timeout: memoising on the *first* call froze whatever
# ``proxy.timeout`` happened to be live at boot, so editing it in the GUI wrote
# to disk, read back correctly and changed nothing until the pod was restarted.
_client: httpx.AsyncClient | None = None
_client_timeout: float | None = None
# Keep a reference to the close task; a bare create_task() may be garbage
# collected before it runs.
_closing: set[asyncio.Task[None]] = set()


def _get_client(timeout: float = 86400) -> httpx.AsyncClient:
    global _client, _client_timeout  # noqa: PLW0603
    if _client is not None and not _client.is_closed and _client_timeout == timeout:
        return _client

    superseded = _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
    _client_timeout = timeout

    if superseded is not None and not superseded.is_closed:
        # Only reached when the operator actually changed the timeout, so the
        # cost (any stream still running on the old client is cut, and the
        # connector reconnects) is paid once per edit rather than per request.
        try:
            task = asyncio.get_running_loop().create_task(superseded.aclose())
        except RuntimeError:  # no loop — nothing is in flight either
            pass
        else:
            _closing.add(task)
            task.add_done_callback(_closing.discard)

    return _client


@router.api_route(
    "/private_{token}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def mcp_proxy(token: str, path: str, request: Request) -> StreamingResponse:
    """Validate the URL-path token and reverse-proxy to the MCP server."""
    store = get_config_store()
    expected_token = await store.get("mcp_auth_token", "")

    # ``secrets.compare_digest`` instead of ``!=``: the path token is the ONLY
    # thing standing between the public tunnel hostname and a live Podman
    # socket, and a short-circuiting comparison leaks its prefix through
    # response timing to anyone who can retry.  The emptiness check stays
    # separate so an unset token fails closed rather than matching "".
    if not expected_token or not secrets.compare_digest(token, expected_token):
        raise HTTPException(status_code=403, detail="Invalid or missing MCP auth token")

    # Build upstream URL
    mcp_cfg = await store.get("mcp_server", {})
    mcp_port = mcp_cfg.get("port", 8000)
    upstream_url = f"http://127.0.0.1:{mcp_port}/{path}"

    # Forward query string
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Build headers — forward most, drop hop-by-hop
    forward_headers = {}
    skip = {"host", "connection", "transfer-encoding", "keep-alive"}
    for key, value in request.headers.items():
        if key.lower() not in skip:
            forward_headers[key] = value
    # The upstream's OWN authority, not the client's and not a bare "localhost".
    #
    # The MCP SDK auto-enables DNS-rebinding protection whenever the server
    # binds to a loopback address — which is exactly how this child is always
    # started — with allowed_hosts ``["127.0.0.1:*", "localhost:*", "[::1]:*"]``.
    # Every one of those patterns requires a colon and a port, so the previous
    # port-less ``"localhost"`` matched none of them and the child answered
    # ``421 Misdirected Request`` to every connector call. Forwarding the
    # client's Host is equally wrong: behind a tunnel it is the public
    # hostname, which the child has never heard of.
    forward_headers["host"] = f"127.0.0.1:{mcp_port}"

    # Add bearer token if configured (optional StaticTokenVerifier gate
    # on the FastMCP side; off by default since the loopback bind plus
    # the URL-path token already isolate the child).
    proxy_cfg = await store.get("proxy", {})
    bearer = proxy_cfg.get("bearer_token")
    if bearer:
        forward_headers["authorization"] = f"Bearer {bearer}"

    # Read body
    body = await request.body()

    # Coerce: a hand-edited config.json can hold "86400" as a string, and
    # httpx.Timeout would raise on it *inside* the request path.
    try:
        timeout = float(proxy_cfg.get("timeout") or 86400)
    except (TypeError, ValueError):
        timeout = 86400.0
    client = _get_client(timeout)

    try:
        upstream_req = client.build_request(
            method=request.method,
            url=upstream_url,
            headers=forward_headers,
            content=body if body else None,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        raise HTTPException(502, detail="MCP server not reachable")
    except httpx.TimeoutException:
        raise HTTPException(504, detail="MCP server timed out")

    # Stream response back — disable buffering for SSE
    resp_headers = dict(upstream_resp.headers)
    for h in ("transfer-encoding", "content-encoding", "content-length"):
        resp_headers.pop(h, None)
    resp_headers["x-accel-buffering"] = "no"

    # Keep upstream redirects inside the proxy's namespace.  FastMCP answers a
    # request for ``/mcp`` with a 307 to ``/mcp/`` and builds that Location from
    # the upstream Host header, i.e. ``https://localhost/mcp/`` — an address on
    # the *client's* own loopback, with the ``/private_{token}`` prefix gone.
    # Any client that normalises the trailing slash away therefore lost the
    # session before it started.  Rewrite the Location to a relative path under
    # this proxy so the redirect lands back here, token intact.
    location = resp_headers.get("location")
    if location:
        parsed = urlsplit(location)
        if not parsed.netloc or parsed.hostname in {"localhost", "127.0.0.1"}:
            rewritten = f"/private_{token}/{parsed.path.lstrip('/')}"
            resp_headers["location"] = urlunsplit(
                ("", "", rewritten, parsed.query, parsed.fragment)
            )

    async def stream_body():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


# Bare /private_TOKEN (no trailing path) also needs to work
@router.api_route(
    "/private_{token}",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def mcp_proxy_root(token: str, request: Request) -> StreamingResponse:
    """Proxy root path (no trailing slash)."""
    return await mcp_proxy(token, "", request)
