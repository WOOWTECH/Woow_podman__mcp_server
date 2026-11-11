# LiteLLM product contract alignment

## Gateway base URL

Normalize surrounding whitespace, trailing slashes and only an exact origin-root
`/v1` API suffix. `https://host/v1/` becomes `https://host`; `/v1/models` is then
requested once. `https://host/proxy/v1/` becomes `https://host/proxy/v1`; the model
route is `/proxy/v1/v1/models` because `/proxy/v1` is the reverse-proxy mount.
Case-sensitive `/V1` and percent-encoded mount names are preserved. Do not guess
which part of a longer path belongs to a proxy.

The product-only helper is used by the connection router, connection-test target
and child Settings. Empty admin configuration remains allowed; secret preservation
and restart behavior are unchanged. No shared URL helper or other product changes.
URLs containing credentials, query, fragment or invalid ports are rejected before
save/child HTTP. Root `/V1`, prefixed `/proxy/v1` and query/fragment behavior change
from the earlier permissive suffix rule; review such configurations before upgrade.
An older saved mount already stripped by the previous router cannot be recovered
automatically: re-save its intended complete gateway root. Downgrading restores
the old suffix behavior on the next save (and in its connection probe); retain a
configuration backup. A deployment depending on a mount ending in `/v1` needs
a compatible proxy alias before downgrade, or must retain the new wrapper.

Main's UI/connection integration should explain that only origin-root `/v1` is
removed; it must not strip arbitrary prefixed mounts. These tests exercise real
source callbacks against MockTransport, not a deployed reverse proxy or backend.

## Active operations and conservative permissions

| Tool | Reviewed backend behavior | Policy in both children / PaaS API |
| --- | --- | --- |
| `litellm_health` | Can run provider probes (potential charges); may save health history. Cached background results are possible. | Fixed `create`; not available in read-only mode. |
| `litellm_token_counter` | Default local token count, no inference; may fetch images/tokenizers and populate caches. MCP does not set `call_endpoint=true`. | Fixed `create` for the potential active work. |
| `litellm_spend_calculate` | Arithmetic cost estimate, no inference/billing request; message token counting may fetch images/tokenizers. | Fixed `create` for the potential active work. |

`create` reuses the existing permission bucket for active work (as chat completion
already does); it is not a claim that the calculators create a database row or
charge for a completion. MCP annotations in both children are conservative:
`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=false`. Simple text
or supplied usage may only require local computation, but backend configuration,
model choice and message content can change that. No per-argument relaxation is
introduced. `health_readiness` remains a read operation.

[Source evidence](parity-source-evidence.json) records SHA-256 and line anchors
for seven matching files in the previously captured PaaS/HA backend patch-chain
snapshots, with the existing bundle's declared LiteLLM 1.104.0/base-image pins.
Those are backend sources, distinct from these MCP wrapper repositories. This is
static source evidence, not a fresh image extraction, live-service test, or proof
of a customer's configured model. [Upstream health documentation](https://docs.litellm.ai/docs/proxy/health)
also explains active health probe charges and the background-cache alternative.

### Upgrade / rollback contract

All-enabled stays at 40 tools. Read-only now exposes 15, previously 18 in the
native/PaaS registry. Automation that used these three through read-only must be
reviewed before an administrator explicitly permits active work. HA's existing
25 exact write grants, defaults and per-tool disables are unchanged; no new HA
grant is created and none is downgraded to read. Handler bodies still use the same
HTTP methods, paths, arguments and response transformations.

Legacy `read` denies remain restrictive for these three names (including bare
`read` in the native environment). PaaS's product adapter supplies
`OperationSpec.legacy_denies={"read": ("create",)}` so the proxy enforces old
saved restrictions immediately; API/UI metadata projects them to the editable
`create` switch. No saved policy is rewritten automatically. Explicit enabling
may still require a child restart to clear its old environment restriction.
Other tools keep their existing labels. The child guard checks again immediately
before HTTP, even when a callback was registered before a deny changed.

Before rollback to a wrapper that classifies these three as read, explicitly
place the three names in that version's whole-tool disable setting and retain a
configuration backup. A new `create` deny alone will not block an old read-labelled
handler. Re-enable only after reviewing the old behavior. No state schema bump or
automatic migration is part of this patch.

### Integration seams owned by main

- Shared connection/UI code must use the product-normalized value and describe
  exact origin-root `/v1` removal; preserve `/proxy/v1` as a mount.
- PaaS Permissions can consume `OPERATION_NOTES`, `OPERATION_POLICIES` and
  `operation_legacy_denies` directly. Show active-work risk rather than implying
  every calculation writes data or charges money; read-only count is 15.
- HA public catalog/provenance and guarded source ledgers need the reviewed
  vendor hashes and updated descriptions/annotations when main integrates.
  The existing shared `surface.py` grants remain authoritative. Root ledgers,
  snapshots, shared UI and versions are deliberately outside this lane.
- Include these behavior changes/rollback steps in the integrated upgrade notes.
  Source tests with MockTransport are not runtime, image or HAOS acceptance.
