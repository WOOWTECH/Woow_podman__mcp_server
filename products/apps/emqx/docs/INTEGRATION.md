# EMQX integration handoff

Base: merged PR95 `3c93d0e727fdc948f1ea17cb5fa7d1ad1887a883`.
The app consumes N13/N15 through the existing shared factory and UI. Connection PUT
uses `run_configuration_transition(..., request=request)`; token/restart routes and
durable receipts remain shared. Tools changes are live, with no app restart middleware.
SQL starts enabled, matching HA's all-on default, and its exact execute grant remains
independently revocable. Tests cover revision preconditions and restart receipt replay.

The authorized root integration adds only:

- `woow-emqx-mcp-server` to `uv.lock`; the existing `apps/*` workspace glob suffices.
- `apps/emqx/frontend` and its link to the root npm workspace/lock.
- EMQX's app tests, changed output/filter, image matrix entry and executable child
  smoke to `.gitea/workflows/ci.yml`.

All existing Python package records (101) and npm package records (176), including
versions, sources and integrity hashes, are preserved. HTTPX 0.28.1/httpcore 1.0.9
source guards remain unchanged. Existing CI jobs, filters and main-only publication
condition are unchanged. Root lock/workflow edits select existing products for their
normal builds under existing filters; no legacy ninth-product migration is implied.
No shared core/UI function, platform template or deployment is changed.

The Dockerfile uses the monorepo root context and the existing baseline image recipe.
Its frontend stage copies the shared UI and EMQX frontend, runs root `npm ci`, then
builds the EMQX workspace. Its Python stage performs two frozen uv installs, before
and after workspace source copy. CI runs the actual image's child smoke via stdin
after `/healthz`, so a zero-configuration admin alone cannot satisfy that check.

`scripts/ci_smoke_child.py` starts an authenticated, owned synthetic HTTP fixture and
the actual MCP child for two generations. It checks allowed/denied effects, live SQL
revocation, saved policy across restart and cleanup. Source tests cover all 39 positive
and denied calls, schemas, strict mixed selectors, authenticated proxy/child processes,
DNS pinning, TLS rejection, bounded responses, redaction, errors and pool lifetime.
These checks do not establish real-broker acceptance, HAOS identity or PVC persistence.

Main owns independent review, actual human CI approval where required, merge and
publication. Do not substitute local installs or smoke for Docker CI. After publication:

1. Record the immutable source tag and registry digest for `emqx-mcp-admin`.
2. Obtain a fresh isolated backend resource lease identifying that exact image, the
   broker image, node UID, CPU/RAM/disk bounds, expiry, isolation and cleanup. No
   cluster resources are authorized by this source integration.
3. Reuse the proven real-EMQX retained-message fixture with synthetic accounts/data:
   permitted publish/read effect; denied unchanged message; backend errors; restart
   readback; deletion of only the owned topic. Add SQL default-enabled, revoke and
   re-enable cases, authenticated app-settings/revision checks, and 0600 configuration
   and durable receipt persistence on storage remount if the lease includes a PVC.
4. Record adapter and server image IDs/digests, source hashes, all failures and owned
   cleanup. Do not equate synthetic HA identity with HAOS ingress or a container
   restart on emptyDir with durable storage remount.

PaaS EMQX remains unavailable until published exact-image acceptance passes. Image
bookkeeping, formal catalog/template exposure and production deployment remain with
main and are outside this change.
