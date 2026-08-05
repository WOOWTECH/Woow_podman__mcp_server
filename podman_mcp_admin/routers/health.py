"""Dashboard health payload — Phase 1 shape.

The key set below is a hard contract with the shared React SPA: ``Dashboard.jsx``
reads ``mcp_server``, ``proxy`` and ``target_app`` unconditionally, so every key
must be *present* even when its value is unknown.  Omitting one crashes the page
on a property access; sending ``null`` renders "N/A", which is the honest answer
for a probe that has not been built yet.

Phase 1 deliberately reports ``target_app`` as all-null: the Podman connection
probe lands in Phase 3 (``podman_client.py`` + ``routers/config.py``).  What IS
real here is ``mcp_server`` and ``proxy`` — the two things that decide whether
the operator's connector works, which is the whole point of getting the console
up early.

Top-level ``db_name`` / ``item_count`` / ``overall_status`` are compatibility
aliases for the other consoles that share this SPA.  Keep them populated; do
not treat them as the contract.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter

from ..net import port_accepts_connections

router = APIRouter(prefix="/api/health", tags=["health"])


def _target_placeholder() -> dict[str, Any]:
    """The ``target_app`` block before the Podman probe exists (Phase 3).

    ``healthy`` is ``False`` rather than ``None`` because the SPA renders the
    card's colour from it directly and ``null`` would paint it green.  The
    ``error`` string says *why* it is not green, so the dashboard does not look
    like a broken Podman host when it is really an unimplemented probe.
    """
    return {
        "healthy": False,
        "url": None,
        "error": "Podman connection probe not implemented yet (Phase 3)",
        "auth_ok": None,
        "version": None,
        "status": None,
        "db": None,
        "model_count": None,
        "container_count": None,
        "image_count": None,
    }


async def _probe_proxy(mcp_port: int) -> dict[str, Any]:
    """Real state of the built-in MCP reverse proxy.

    In the reference this card was a hardcoded ``{"healthy": True}``, so it
    stayed green while ``mcp_admin_core.proxy`` 403'd every
    ``/private_{token}/…`` request for want of a token — i.e. it was green
    exactly when the connector was dead.  Both failure modes are checked here.
    """
    try:
        from mcp_admin_core.config import get_config_store

        token = await get_config_store().get("mcp_auth_token", "") or ""
    except Exception:  # noqa: BLE001 — core absent in unit-test contexts
        token = ""

    if not token:
        return {
            "healthy": False,
            "pod_name": "no MCP auth token — /private_…/mcp/ returns 403",
            "token_configured": False,
            "upstream_reachable": None,
            "error": "No mcp_auth_token configured; every connector request is rejected.",
        }

    reachable = await port_accepts_connections(mcp_port) if mcp_port else None
    if reachable is False:
        return {
            "healthy": False,
            "pod_name": f"upstream 127.0.0.1:{mcp_port} refused",
            "token_configured": True,
            "upstream_reachable": False,
            "error": f"MCP child is not listening on 127.0.0.1:{mcp_port}.",
        }
    return {
        "healthy": True,
        "pod_name": (
            f"built-in reverse proxy → 127.0.0.1:{mcp_port}"
            if mcp_port
            else "built-in reverse proxy"
        ),
        "token_configured": True,
        "upstream_reachable": reachable,
        "error": None,
    }


@router.get("")
async def health() -> dict[str, Any]:
    """Aggregate status for the dashboard: MCP subprocess and proxy."""
    try:
        from mcp_admin_core.process import get_process_manager

        # McpProcessManager.status() is a coroutine in mcp_admin_core.
        status = await get_process_manager().status()
    except Exception:  # noqa: BLE001 — core absent in unit-test contexts
        status = {}

    port = int(status.get("port") or 0)
    proxy = await _probe_proxy(port)

    process_alive = bool(status.get("running"))
    serving = await port_accepts_connections(port) if (process_alive and port) else False
    running = process_alive and (serving or not port)

    target = _target_placeholder()

    return {
        "app_type": "podman",
        # Phase 1: target_app is a known placeholder, so it must not drag the
        # overall status down — otherwise the banner reads "degraded" on a
        # perfectly healthy install and the operator learns to ignore it.
        "overall_status": "ok" if running and proxy["healthy"] else "degraded",
        "mcp_server": {
            "healthy": running,
            "pod_name": f"pid={status.get('pid')}" + ("" if running else " · not serving"),
            "restart_count": status.get("restart_count", 0),
            "exit_code": status.get("exit_code"),
            "port": port or None,
        },
        "target_app": target,
        "proxy": proxy,
        "version": None,
        # Compatibility aliases (see module docstring).
        "db_name": "n/a",
        "item_count": None,
        "namespace": os.environ.get("NAMESPACE", "podman-mcp"),
    }
