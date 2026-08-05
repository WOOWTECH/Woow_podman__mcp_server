"""Clean 404s for OAuth-discovery probes.

Before its first JSON-RPC call an MCP client probes a handful of well-known
OAuth documents to find out whether the server wants an authorization flow.
This server does not: the credential *is* the ``/private_{token}/`` path
segment, and there is no IdP behind it.

The problem was that the SPA catch-all answers every unmatched GET with
``200 text/html``.  A probe for ``/.well-known/oauth-authorization-server``
therefore came back "200 OK" — indistinguishable, to a client, from "yes, I
have an authorization server".  Claude's connector then went on to Dynamic
Client Registration against ``/register``, got the same HTML shell back, and
gave up with *"Couldn't register with … 's sign-in service"*.

Answering those probes with a JSON ``404`` makes discovery fail fast, which is
exactly the signal a client needs to fall back to anonymous access and just
send ``initialize``.  The Cloudflare Worker in ``cloudflare/mcp-direct.js``
does the same thing one hop earlier; this router makes the origin correct on
its own, so the Worker stays optional.

ORDERING: this router must be included *before* the SPA fallback route in
``create_app`` — after it, the catch-all wins and nothing changes.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["discovery"])

_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]

_NOT_FOUND = {"error": "not_found", "error_description": "no OAuth on this server"}


@router.api_route("/.well-known/{path:path}", methods=_METHODS, include_in_schema=False)
async def well_known_not_found(path: str) -> JSONResponse:
    """Every ``/.well-known/*`` probe: a clean, honest 404."""
    return JSONResponse(_NOT_FOUND, status_code=404)


@router.api_route("/register", methods=_METHODS, include_in_schema=False)
async def dcr_not_found() -> JSONResponse:
    """Dynamic Client Registration endpoint — deliberately absent."""
    return JSONResponse(_NOT_FOUND, status_code=404)
