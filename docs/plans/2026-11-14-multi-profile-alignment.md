# Podman multi-profile alignment plan

**Goal:** Give the Podman deployment of the WOOW MCP servers the PaaS
multi-account model (多帳戶／多 API 兼容). One MCP instance serves several
fixed backend accounts. Each account has its own backend API endpoint,
credentials and API version, and its own token URL, tool policy and child
process, as specified for PaaS in `woow-mcp-server`
`docs/plans/2026-11-11-mcp-multi-profile-paas.md`.

**Status:** planning record, 2026-11-14. Each phase below is one PR. Tests
run on throwaway stacks with generated credentials. A PR merges only on the
owner's explicit instruction, and any change to a running host needs a
separate explicit OK.

## Owner decisions (2026-11-14)

1. Scope: both parts of this repository. The vendored products come
   first, then the root Podman host MCP.
2. "多 API 兼容" means a per-account backend API endpoint, credentials and
   API version, each behind its own token URL. Choosing an endpoint per call,
   the Docker-compatible API and an ssh transport stay out of scope unless the
   owner asks for them later.
3. Phase 1 is prepared against PaaS `443e95a`. It merges only after the owner
   confirms the review of Gitea #140-#146.
4. Changes to vendored product source land in PaaS first and arrive through a
   refresh (#13, #19, #24, #25). #21 is closed as superseded by #23. #17 is
   replaced by the Phase 3 release record. #26 keeps its own release
   conditions; whichever of #26 and the Phase 1 refresh merges second
   regenerates the source snapshot.
5. VK is retired from the Podman products; the owner no longer uses it.
   Its source stays vendored unchanged for the PaaS workspace locks, and no
   VK image is built or installed. Removing the running VK service from a
   host is a separate, explicitly approved step.
6. Before the Phase 3 release, `products/` is refreshed again to PaaS
   `5a06d53` (#147, #148, #150), so the first rollout already carries the
   gateway queue fixes. It merges only after the owner confirms the review
   of those PRs.

## Starting point

- `products/` pins PaaS `0e432c3`, which already contains the multi-profile
  core. Product images built before that change do not offer profiles.
- PaaS `main` has since merged #140-#146: a security release, a Logs buffer
  per profile, profile robustness, log redaction, a per-profile gateway quota
  and the restore runbook.
- The root Podman host MCP serves one account. It runs on MCP SDK 1.x and an
  older copy of the admin core that has no profile support.

## Invariants (from the PaaS plan, applied to Podman)

- A token selects exactly one profile. Request arguments and headers cannot
  change the backend identity.
- The default profile, its existing `/private_<token>/mcp` URL and the admin
  login keep working.
- Rotating or revoking a token ends only that profile's streams. Sessions
  cannot move between profiles.
- Backend credentials never reach MCP clients or logs.
- No child process starts from a user-supplied command.
- Podman host MCP: a profile without a saved endpoint never falls back to
  another endpoint or to the host socket.

## Phases

Products track:

0. This record.
1. Refresh `products/` to the approved PaaS commit: vendored source,
   provenance and source snapshot. No host change. A second refresh (1b)
   moves the pin to `5a06d53` before the release (decision 6).
2. Podman deploy layer for several profiles: a container memory ceiling so
   the core's capacity check applies, a restore that leaves every profile
   token revoked, validated Origin and gateway settings, a second-profile
   container smoke test, and no restart loop on the core's deliberate exits.
3. Release and gated rollout of the products. Needs a separate OK. The owner
   chose an unsigned release (2026-11-14): `--skip-release-signature` with
   images built from merged `main`. Done on 2026-11-14: all nine products run
   `bd6ba26`, see `products/deploy/releases/2026-11-14-multi-profile-rollout.md`.

Podman host MCP track:

4. Child on MCP SDK 2.x with Streamable HTTP and SSE. It refuses to start
   without an explicit endpoint. Unit tests plus an integration test against
   two throwaway Podman API endpoints.
5. Admin rebuilt on the vendored shared core, with one profile per Podman API
   endpoint.
6. Shared admin UI with the profile chooser, plus browser e2e.
7. Packaging, Quadlet, release gate, secret handling and an explicit mount
   allowlist.
8. Live install and connector cutover. Needs a separate OK.

Optional, only on request:

9. Several endpoints behind one URL, the Docker-compatible API, or ssh.

## Open questions for later phases

- Which Podman accounts to support: the same user's socket, other local
  users' sockets, or remote endpoints over https with client certificates.
- The memory ceiling per product container on the target host.
- Where the host app lives: at the repository root on the vendored core, or
  upstream in PaaS.
