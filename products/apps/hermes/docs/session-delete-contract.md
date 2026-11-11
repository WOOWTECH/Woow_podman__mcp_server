# Session deletion against the published Agent

The exact published Hermes Agent image `20261025.0421@sha256:39718ca149934e1c4da240a5f41b53d45e0d5b62dba508b79c32fb0ef1aaf0f0` rejects the previous adapter's bare `DELETE /api/sessions/{id}` with HTTP409 `client_upgrade_required`. The adapter now discovers the mutation contract through a fresh authenticated `GET /api/sessions/{id}/mutation-snapshot`, reads the authoritative revision and generation, and sends one delete with those fences and a new UUID request ID.

The published HTTP parameters are optional for compatibility/error reporting, but the native runtime requires **both revision and generation** for a delete. Missing, malformed or foreign snapshots never produce an unfenced delete. An absent session is reported as not found without sending a delete. A successful acknowledgement is followed by another snapshot to verify absence.

The adapter does not retry a delete, refresh fences after a conflict, or treat a timeout as a failed write. A lost reply, server error, malformed acknowledgement or unavailable absence check returns `SESSION_DELETE_OUTCOME_UNKNOWN` and the exact request identity. The caller must inspect before choosing to retry. For an explicit replay, pass `request_id`, `expected_revision` and `expected_generation` together and unchanged. The adapter keeps them even when the fresh snapshot is absent or changed; the Agent's authenticated durable receipt/CAS determines whether to replay or refuse. No adapter-side receipt cache or shared-core change is needed.

The tool count remains 11. Normal delete calls and their success shape remain unchanged. Three optional identity fields support explicit recovery; they are invalid on list/get and partial/malformed identities are rejected before network I/O. Operation grants remain enforced by the existing `hermes_session` action policy.

Backends without the mutation-snapshot contract now fail closed for deletion. There is no implicit fallback to a legacy unfenced write. This is a deliberate compatibility limit requiring review, not a claim that older Agent deletion was tested or retained. List/get and other tools keep their existing behavior.

Published contract source evidence:

| Path in Agent image | SHA256 |
|---|---|
| `opt/hermes/hermes_cli/web_routers/sessions.py` | `f1850450cb3060f211075cb5a884ca5307509ea570767c1efe4d9bbe70d9a66b` |
| `opt/hermes/hermes_cli/web_server_sessions.py` | `14a9d412ca3d9a6b000939813a41784116e48c5fe1a8358d3782f1499e08fd7e` |
| `opt/hermes/hermes_state_runtime.py` | `1553a9e5091413092dd30d7613b9aaa7338919bfc5a5d3ac3f4cf8c57de4411b` |

The separate immutable MCP95/Agent R3 fixture completed on 2026-10-30. Eight phases passed: configuration, native two-turn conversation/model writes, denied writes, errors, child restart, persistence, and two remounts of the separate app/Agent PVCs. The final DELETE returned HTTP409 without deleting the owned session. Product compatibility remains failed for that published image. All owned Kubernetes and Longhorn resources were independently verified absent afterward. This used the real unchanged Agent with deterministic inference, not an external LLM.

The shared `packages/mcp-admin-core/tests/test_hermes_functional.py` fixture now models the published snapshot/fence/receipt contract, with nonzero revision and generation. Two added cases reject stale revision or generation with no effect and one DELETE attempt. The app tests also cover wrong-identity receipt replay, missing contracts, lost replies, strict MCP inputs and verified absence. The combined app, functional and security suite passes 294 tests, including all 54 app tests (24 new contract cases).

Local HTTP-contract tests do not establish published-fixed-image acceptance. The frozen R3 run used the previous MCP95 image and no runtime overlay. After normal review/build/publication, the fixed image needs its own exact-image acceptance and a new MAIN resource lease. No shared runtime, UI, dependency lock or HA release source is changed here.

The session DELETE marks its HTTP request with the local `hermes_no_auth_retry`
extension. The existing Dashboard cookie-refresh wrapper returns a401 for that
request without refreshing or resending it; default and explicitly supplied
mutation identities follow the same rule. This extension is not sent as a header
or query parameter. Read requests still refresh a rejected cookie at most once.
Tests exercise the real authenticated-client wrapper as well as the mutation
helper, so a hidden authentication retry cannot satisfy the one-request contract.
