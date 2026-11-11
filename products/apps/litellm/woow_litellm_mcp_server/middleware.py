"""FastMCP middleware that makes gated tools *explain themselves*.

Registration-time gating (see :mod:`woow_litellm_mcp_server.gating`) removes a
disabled tool from the server entirely, which is the right security posture:
``tools/list`` shrinks and there is no handler left to call. The cost is the
error a client gets when it calls one anyway — which happens constantly in
practice, because MCP clients cache the tool list and an administrator can flip
a switch in the admin console at any moment. FastMCP answers such a call with::

    Unknown tool: 'litellm_generate_key'

That message is actively misleading. It reads as "no such tool exists" — so a
model retries with a guessed name, or reports the server as broken — when the
truth is "this tool exists and an administrator switched it off".

:class:`GatingMiddleware` runs on ``tools/call`` *before* FastMCP resolves the
name, so for any name that is in the registry but not currently exposed it
raises a ``ToolError`` carrying
:meth:`~woow_litellm_mcp_server.gating.ToolGate.explain_disabled`'s reason
(category off / individually disabled / read-only / operation policy) plus the
advice to refresh ``tools/list``. Names that are not in the registry fall
through untouched and still get the default unknown-tool error, because for
those it is the correct answer.

``ToolError`` is deliberate: it is the one exception class FastMCP forwards to
the client verbatim even with ``mask_error_details=True``, so the explanation
survives masking while genuine internal failures stay masked.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import ToolError
from .gating import ToolGate

logger = logging.getLogger(__name__)

try:  # FastMCP >= 2.9 ships the middleware base class.
    from fastmcp.server.middleware import Middleware  # type: ignore

    _HAS_MIDDLEWARE = True
except Exception:  # pragma: no cover - very old fastmcp
    Middleware = object  # type: ignore[assignment, misc]
    _HAS_MIDDLEWARE = False


class GatingMiddleware(Middleware):  # type: ignore[misc]
    """Turn "Unknown tool" into "this tool is disabled, and here is why"."""

    def __init__(self, gate: ToolGate) -> None:
        self._gate = gate

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        name = getattr(getattr(context, "message", None), "name", None)
        if isinstance(name, str):
            reason = self._gate.explain_disabled(name)
            if reason:
                logger.info("Refused gated tool call %r: %s", name, reason)
                raise ToolError(reason)
        return await call_next(context)


def install(mcp: Any, gate: ToolGate) -> bool:
    """Attach :class:`GatingMiddleware` to ``mcp``; return whether it stuck.

    Non-fatal by design — the server must still boot on a FastMCP build with no
    middleware support or a renamed hook. Losing the middleware only costs the
    friendlier message; the gate itself is unaffected because it already ran at
    registration time.
    """
    if not _HAS_MIDDLEWARE or not hasattr(mcp, "add_middleware"):
        logger.warning(
            "FastMCP build has no middleware support; gated tools will report "
            "the generic 'Unknown tool' message."
        )
        return False
    try:
        mcp.add_middleware(GatingMiddleware(gate))
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not install GatingMiddleware", exc_info=True)
        return False
    return True
