"""FastMCP server exposing a Podman host through its libpod REST API.

Phase 1 keeps the whole tool surface in a single :mod:`~woow_podman_mcp_server.server`
module, deliberately.  It is the file that has been running in production and
has the empirically-verified libpod quirks baked into it (multiplexed framing,
``stats`` returning HTTP 200 on failure, ``top`` column mismatches); splitting it
into ``tools/`` before those behaviours are covered by tests would be reshuffling
code nobody can yet verify.  The split lands in Phase 2 alongside ``registry.py``
and ``gating.py``.

Import ``mcp`` from here to embed the server; run ``server.main()`` to serve it.
"""

__version__ = "0.1.0"
