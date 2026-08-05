"""Loopback reachability probe for the MCP child process.

In the litellm original this lived in ``routers/tools.py``, so ``health.py``
imported the whole tool-management router just to reach one socket helper.
Phase 1 ships no tools router, and health still needs the probe, so it lives on
its own here — where Phase 2's ``routers/tools.py`` will import it from too.
"""

from __future__ import annotations

import asyncio

# Two seconds is generous for a loopback connect and still short enough that a
# hung probe cannot stall the dashboard behind it.
_PROBE_TIMEOUT_SECONDS = 2.0


async def port_accepts_connections(port: int) -> bool:
    """True when something is listening on the child's loopback port.

    A live pid is not the same as a serving child: the process may still be
    importing its dependencies, or already dead but not yet reaped.  Every
    "is the MCP server up?" answer in this package is decided by this probe
    rather than by ``pid is not None``.
    """
    if not port:
        return False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001 — closing a probe socket must never fail
        pass
    return True
