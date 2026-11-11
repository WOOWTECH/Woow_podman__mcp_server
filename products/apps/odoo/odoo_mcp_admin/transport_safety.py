"""Prevent implicit XML-RPC replay before OdooClient's read-only retry policy.

The pinned upstream ``RedirectTransport.request`` calls the stdlib
``Transport.request``. That implementation silently sends a request twice on
``RemoteDisconnected`` or a connection reset, even if the server has already
committed a write. The same upstream transport also follows redirects by
resending the request. One exchange per transport call leaves retries solely
to ``OdooClient._execute`` and its reviewed read-only method allowlist.
"""

from __future__ import annotations

import hashlib
import http
import xmlrpc.client
from pathlib import Path

import odoo_mcp.odoo_client as client

ODOO_CLIENT_SHA256 = "3cb7796965772bbea02fa1fda5c4de1fdcb36055ae1dcf9db396fd22559ff6c7"


class BackendHTTPError(ValueError):
    """A non-200 answer. A ValueError, like HA's XMLTransport: OdooClient._execute retries only connection
    drops and timeouts, never an HTTP status. Its text is the HA add-on's public text:
    ``BACKEND_HTTP_ERROR status=<n>: <standard reason phrase>``. No URL, no header (a redirect's Location)
    and no backend-supplied reason text."""

    def __init__(self, errcode):
        self.errcode = int(errcode)
        super().__init__(self.errcode)

    def __str__(self):
        try:
            reason = http.HTTPStatus(self.errcode).phrase
        except ValueError:
            reason = ""
        return f"BACKEND_HTTP_ERROR status={self.errcode}: {reason}"


def _request_once(self, host, handler, request_body, verbose=False):
    """One XML-RPC exchange; redirects and ambiguous disconnects propagate.

    Failures carry the HA add-on's public code prefix (apps/runtime/backend_policy.py XMLTransport), so both
    products return the same text for the same failure class: BACKEND_TIMEOUT, BACKEND_UNAVAILABLE (also any
    other transport or parse failure), BACKEND_HTTP_ERROR status=<n>. Odoo's own Fault passes unchanged.
    """
    try:
        return self.single_request(host, handler, request_body, verbose)
    except TimeoutError as exc:
        raise TimeoutError(f"BACKEND_TIMEOUT: {exc}") from None
    except OSError as exc:
        # Same type family as before, so the reviewed read-only retry still applies.
        raise ConnectionError(f"BACKEND_UNAVAILABLE: {exc}") from None
    except xmlrpc.client.ProtocolError as exc:
        raise BackendHTTPError(exc.errcode) from None
    except xmlrpc.client.Fault:
        raise
    except Exception as exc:
        raise ValueError(f"BACKEND_UNAVAILABLE: {exc}") from None


def install() -> None:
    """Patch the reviewed upstream transport before any OdooClient is made."""
    if (
        hashlib.sha256(Path(client.__file__).read_bytes()).hexdigest()
        != ODOO_CLIENT_SHA256
    ):
        raise RuntimeError("Odoo XML-RPC transport requires source review")
    transport = client.RedirectTransport
    if transport.request is _request_once:
        return
    if (
        transport.__module__ != client.__name__
        or transport.request.__module__ != client.__name__
    ):
        raise RuntimeError("Odoo XML-RPC transport was modified before safety install")
    transport.request = _request_once
