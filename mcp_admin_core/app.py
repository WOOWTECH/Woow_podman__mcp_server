"""FastAPI application factory for MCP Admin GUI services."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Sequence

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .auth.middleware import AuthMiddleware, login_router
from .discovery import router as discovery_router
from .process import get_process_manager
from .proxy import router as proxy_router
from .routers.settings import router as settings_router

logger = logging.getLogger(__name__)


def create_app(
    title: str,
    extra_routers: Sequence[APIRouter] | None = None,
    *,
    static_dir: str | Path | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Create and configure a FastAPI application.

    Parameters
    ----------
    title:
        The application title shown in the OpenAPI docs.
    extra_routers:
        Additional ``APIRouter`` instances to include in the app.
        NOTE: the SPA catch-all route mounted at the end of this
        factory shadows any router included *after* it, so product
        routers MUST be passed in here (they are included before the
        static fallback).  This is a load-bearing ordering gotcha.
    static_dir:
        Path to a directory of static files to serve.
        Defaults to ``./static`` if it exists.
    cors_origins:
        Explicit list of allowed CORS origins.  Empty/omitted (the default)
        means no CORS middleware is installed at all — correct for the
        same-origin console.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: start MCP server if configured
        pm = get_process_manager()
        await pm.start()
        yield
        # Shutdown: stop MCP server
        await pm.stop()

    app = FastAPI(title=title, lifespan=lifespan)

    # -- Auth middleware -------------------------------------------------------
    app.add_middleware(AuthMiddleware)

    # -- CORS ------------------------------------------------------------------
    # ``allow_origins=["*"]`` together with ``allow_credentials=True`` is a
    # combination browsers reject outright — the wildcard is not echoed back for
    # a credentialed request, so the fetch fails CORS rather than succeeding
    # permissively.  It bought nothing and hid real errors.  The console is
    # same-origin (SPA and API on one port), so the default is now NO CORS
    # middleware at all; a caller that genuinely needs cross-origin access
    # passes explicit origins.
    origins = list(cors_origins or [])
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # -- Core routers ----------------------------------------------------------
    app.include_router(login_router)
    app.include_router(settings_router)
    app.include_router(proxy_router)
    # OAuth-discovery 404s — must precede the SPA fallback below, otherwise the
    # catch-all answers the probes with 200 HTML and MCP clients try to run an
    # OAuth registration that does not exist here.  See discovery.py.
    app.include_router(discovery_router)

    if extra_routers:
        for router in extra_routers:
            app.include_router(router)

    # -- Health endpoint (before SPA fallback) ---------------------------------
    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -- Static files + SPA fallback (must be last) ----------------------------
    if static_dir is None:
        static_dir = Path.cwd() / "static"
    else:
        static_dir = Path(static_dir)

    if static_dir.is_dir():
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_html = static_dir / "index.html"
        if index_html.is_file():
            _index_content = index_html.read_text()

            @app.get("/{path:path}", include_in_schema=False)
            async def spa_fallback(path: str) -> HTMLResponse:
                return HTMLResponse(_index_content)

        logger.info("Serving SPA from %s", static_dir)
    else:
        logger.debug("No static directory found at %s; skipping", static_dir)

    logger.info("Created application '%s'", title)
    return app
