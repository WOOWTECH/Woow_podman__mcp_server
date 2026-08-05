"""Podman MCP Admin — GUI, Admin API and MCP proxy on a single port.

Mirrors ``litellm_mcp_admin.main`` / ``emqx_mcp_admin.main``: ``mcp_admin_core``
supplies the FastAPI factory, JWT middleware, config store, subprocess manager
and MCP reverse proxy; this module only contributes the Podman-specific
routers.

Phase 1 wires up ``health``, ``logs`` and ``tokens`` only.  ``config`` (the
connection probe) and ``tools`` (profile/tool gating) arrive in Phase 3 and
Phase 2 respectively; until then the ConnectionConfig and ToolManager pages get
a JSON 404 from ``api_fallback`` and render empty, which is deliberate and much
easier to debug than a stubbed router that pretends to work.

``extra_routers`` MUST be passed to the factory — routers added after
``create_app()`` returns are shadowed by the SPA catch-all route (the
load-bearing gotcha preserved from the reference).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from mcp_admin_core.app import create_app

from .routers import health, logs, tokens

# The SPA fallback (``@app.get("/{path:path}")``, registered last by the
# factory) matches *everything*, so a typo'd or removed API path answered 200
# with the HTML shell. `fetch('/api/toolz').then(r => r.json())` then failed on
# "Unexpected token '<'" and the page showed a parser error instead of "404".
# Because Starlette only converts a method mismatch into 405 *after* scanning
# every route, this catch-all would also swallow those 405s — so it reproduces
# them itself, Allow header included.
api_fallback = APIRouter(include_in_schema=False)

_API_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def _iter_routes(routes) -> "list":
    """Flatten ``app.routes``.

    FastAPI >= 0.116 stores an included router as a single ``_IncludedRouter``
    wrapper rather than splicing its routes into ``app.routes``, so a flat scan
    finds no paths at all and every method mismatch would report 404.
    """
    flat = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            flat.extend(_iter_routes(inner.routes))
        else:
            flat.append(route)
    return flat


def _methods_for_path(request: Request) -> set[str]:
    """Methods some *other* route would accept for this exact path."""
    allowed: set[str] = set()
    path = request.url.path
    for route in _iter_routes(request.app.routes):
        if getattr(route, "endpoint", None) is api_not_found:
            continue  # ourselves — we accept everything and prove nothing
        regex = getattr(route, "path_regex", None)
        if regex is None or not regex.fullmatch(path):
            continue
        allowed |= set(getattr(route, "methods", None) or set())
    return allowed


@api_fallback.api_route("/api/{rest:path}", methods=_API_METHODS)
async def api_not_found(request: Request, rest: str) -> JSONResponse:
    """JSON 404/405 for anything under /api the real routers did not claim."""
    allowed = _methods_for_path(request)
    if allowed:
        return JSONResponse(
            {"detail": f"Method {request.method} not allowed for {request.url.path}"},
            status_code=405,
            headers={"Allow": ", ".join(sorted(allowed))},
        )
    return JSONResponse(
        {"detail": f"Unknown API endpoint: {request.url.path}"}, status_code=404
    )


app = create_app(
    title="Podman MCP Admin",
    extra_routers=[
        tokens.router,
        health.router,
        logs.router,
        # LAST of the extra routers: every real /api route above must be
        # matched before this one gets a chance to answer 404.
        api_fallback,
    ],
)


@app.middleware("http")
async def no_store(request, call_next):
    """Keep the admin UI out of every cache in front of it.

    Behind Cloudflare a single 404 from before the tunnel route existed got
    cached at the edge and kept serving long after the origin was healthy.
    Nothing here is cacheable anyway: the SPA shell is tiny and every API
    response is live state.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response
