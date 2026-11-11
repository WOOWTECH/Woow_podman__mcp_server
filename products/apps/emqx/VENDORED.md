# EMQX source and adaptation provenance

The starting implementation is WOOWTECH's MIT-licensed
`Woow_emqx_mcp_server@1be17bad5aef6c7b7519686ccbfe1d80762bc10e`, taken from
`Woow_ha_mcp_addons@28b8317ec12c47c81c946fe9aaec18dd1d5ad852` under
`apps/emqx/vendor/emqx_mcp_server/`. The original copyright/license is preserved
in [LICENSE](LICENSE). No production credentials or customer data are included.

[source-manifest.json](docs/source-manifest.json) records each starting HA file's
SHA-256 and the adapted package SHA-256. This is a source manifest, not an OCI image
attestation. Original PaaS base: `7f0eb78f6edec39166b74d88a53307c4614dcf35`;
current integration base: merged PR95 `3c93d0e727fdc948f1ea17cb5fa7d1ad1887a883`.

This package is an explicitly adapted copy, **not byte-identical vendor source**:

- `http_policy.py` is the app-owned HTTP transport. Its reviewed HTTPX/httpcore
  pool/address-pinning technique and dependency source hashes follow the HA runtime
  transport; actual destination rules come from PaaS core SSRF policy. There is no
  `backend_policy` shim or permissive transport substitution.
- `lifespan.py` owns that real protected client and closes its pool on shutdown.
- `server.py` registers the complete surface and adds live saved-policy middleware.
  Environment registration gates are not the PaaS authorization source.
- `settings.py` removes dotenv and hidden localhost destination fallback.
- `errors.py` supplies bounded generic HTTP failures and rejects malformed/empty
  HTTP 200 JSON. Explicit 204 remains valid for no-content responses.
- `tools/integration.py` marks SQL testing as write, matching HA's exact grant
  semantics. The grant is enabled by the all-on default, not default-off.
- `tools/messaging.py` propagates retained lookup failures except 404 and removes
  the dangerous publish fallback after failed deletes. Regression tests first
  reproduced two requests on a 503; the adapted behavior makes one request.
- `tools/security.py` avoids reflecting broker-provided source names in errors.

The new admin package, tests, app-specific frontend, Docker recipe and smoke script
are PaaS integration work. Tests exercise the pinned APIs with installed Python
3.12.14, FastMCP 4.1.3, MCP 2.3.0, HTTPX 0.28.1 and httpcore 1.0.9. Root locks remain
main's responsibility. Do not update the HTTP dependencies or copy an HA stub to
bypass source-hash failures; review the changed transport and rerun socket tests.
