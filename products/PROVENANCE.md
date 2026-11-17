# MCP product source

## Ownership since 2026-11-17

This repository owns the product source. The nine active `products/apps/*`
trees and `products/packages/mcp-admin-core` and `mcp-admin-ui` were copied
from PaaS `WOOWTECH/woow-mcp-server` `main` commit
`48f394d6fbce3c38a22b75f06e51ec208f230d91` (Gitea, 2026-11-16), the fork base.
On 2026-11-17 the owner stopped syncing them from PaaS (decision 10 in
`docs/plans/2026-11-14-multi-profile-alignment.md`). Changes to the product
source are made and reviewed in pull requests here. A PaaS change comes over
only when someone ports it by hand.

- `deploy/catalog.json` keeps the fork base in `paas_source_commit`. Release
  images carry it in the `io.woowtech.canonical-source` label, and the shared
  image version keeps the form `paas-48f394d-podman.N`. A release that changes
  product source raises `N`. Headscale keeps its own `VERSION`.
- CI runs `scripts/check-release-wiring.py`, which replaces the vendored-source
  snapshot. It checks the release image bindings, not the source.
- `scripts/check-all-parity.py` and `scripts/check-headscale-parity.py` compare
  with a PaaS checkout of the fork base and list every file that differs. CI no
  longer runs them. The signed release path, dormant while releases are
  unsigned, still calls them, so it needs a redesign before a signing key is
  used again.
- The HA tool-name check described below still runs in CI. A change that adds
  or renames a tool must update it.

### Porting a PaaS change by hand

The GitHub mirror of PaaS keeps following Gitea, so it is a convenient source.

1. Apply the change's diff under `products/`, for example
   `git -C /path/to/woow-mcp-server diff BEFORE AFTER -- apps packages | git apply -3 --directory=products`.
2. Run the affected product tests, open a pull request here, and add an entry
   to the list below.

### Changes made here since the fork

- 2026-11-17, Hermes cron (shared images `paas-48f394d-podman.2`): a job
  created with `enabled=false` is checked against the state Hermes saved. If it
  still came out enabled, it is paused, or deleted when the pause fails, and the
  create is never resent. `update` takes `enabled` and pauses or resumes the job
  through the pause and resume endpoints only. Taken from the PaaS mirror's PR
  #6 (commit `f82f2aa`, on top of the fork base), which PaaS has not merged.

## History before the fork

The fork base `48f394d6fbce3c38a22b75f06e51ec208f230d91` (Gitea, 2026-11-16)
was the last PaaS pin. Since the
previous pin `5b84b5d0fc70ab96a580629f1377ab65159ea96d`, Gitea merged #155
(gateway request-bytes budget: request bodies over 64 KiB count against a
gateway-wide and a per-profile byte budget derived from the container memory
limit, uploads have idle and total deadlines, every product gets a JSON
structure guard that refuses bodies nested too deeply or with too many
elements before they are parsed, and non-UTF-8 bodies are refused), #135 (EMQX: no
idle keep-alive connections, so the first request after a network recovery no
longer waits for a stale socket), #139 (LiteLLM: the same for the gateway
client), #138 (Hermes: a cron job created with `enabled=false` is created
paused) and #156 (Hermes: the cron tool description says so, and that
Hermes v0.22.x resumes a paused job when it is triggered). The pin before,
`7d111b175fbbcdf692ff579cc5793bf8c7de3a7b`, was followed by #151
(VK removed from the workspace, its tests and the uv and npm locks), #152
(Nextcloud: the connection URL passes the core SSRF check when it is saved,
on Test connection and in the health probe, and masked log lines keep their
argument shape), #153 (EMQX: the `match_topic` description follows the
broker, Prometheus stats are requested as text, and over-long identifiers
return a fixed error code) and #154 (Nextcloud: changing the user or the URL
revokes the old MCP URL and issues a new token, as Odoo does). The pin
before, `5a06d533eb433ad511bba8e2db946a0de922925d`, was followed by #149
(log redaction: credential values in URL query parameters, such as `?token=`
between encoded separators and keys like `api_key`, `key`, `secret`,
`password` and `sig`, no longer reach container stdout or the Logs page, and
lines with many such segments are masked faster). The pin before,
`443e95a942c7ef0484d963631382aab94d0e0567`, was followed by #147
(restore runbook follow-ups), #148 (gateway queue: a queued write no longer
runs after its client disconnects, queue limits, notifications are not
turned into 503, `-32004` names its cause, redirect `Location` rewriting keeps
clients under their `/private_` path, and an over-long child output line no
longer stalls the log reader) and #150 (documentation and test follow-ups to
#148). The first pin, `0e432c3ebd7a481b8994105a187f29837d941fd6`, was
followed by #140 (security release: Origin report-only default, restore
without old tokens, child identity checks), #141 (Logs buffer per profile),
#142 (profile management and startup robustness), #143 and #146 (restore
runbook and Headscale restore), #144 (redaction of encoded `/private_` paths)
and #145 (per-profile gateway connection quota, clean errors when a child
exits or a token changes mid-response). None of these refreshes changes a
tool registry, lock file, Dockerfile or package manifest. The Headscale app
implements the Headscale v0.29.4 29-operation native contract, its dedicated
management GUI, operation policy, Origin checks, session revocation, and the
optional PaaS binding. The HA Add-on's Headscale child came from the same
source family at `d7c78aaeff001f5fcf87f69ecd3db290396a5906`, with HA-specific
authorization and ingress adaptation.

At the fork base, the only Headscale application/core source edit was
`deployment_kind="podman"` in `apps/headscale/headscale_mcp_admin/main.py`.
For the other eight products, application and UI source was identical except:

- each `main.py` reports `deployment_kind="podman"`;
- each application Dockerfile starts through the shared first-boot bootstrap;
- the K8s Dockerfile verifies the same pinned SHA256 after downloading the Go
  binary because Podman 4.9 Buildah cannot parse remote `ADD --checksum`;
- three test expectations refer to Podman deployment identity, the Podman CI
  workflow, or an unused loopback port on a host already running services.

The shared Quadlet installer, first-boot credential seeding, and rootless
volume ownership live in `products/deploy/shared` and do not change any MCP
operation or GUI code. The archived `odoo-manage` frontend directory is present
only to satisfy the PaaS npm workspace lock and is not a deployed product.
VK was retired from the Podman products on 2026-11-14. PaaS removed `apps/vk`
and its lock entries in #151, so the refresh to `5b84b5d` removed the vendored
copy as well.
`docs/paas-config-restore-runbook.md` is the PaaS Kubernetes restore runbook,
vendored unchanged only because the shared core tests read it; it does not
describe a Podman procedure.

The root Podman host MCP, which used MCP SDK 1.x, was retired on 2026-11-14.
MCP products use this `products/` workspace and the shared admin core
copied from PaaS. The Headscale product resolves the
listed protocol/security dependency versions from the PaaS lock; the local
`uv.lock` records the full Podman product graph.

Until the fork, every refresh had to pass both parity scripts against a clean
checkout of the new pin, and CI checked a snapshot of the vendored files
(`source-parity-snapshot.json`), because GitHub-hosted runners cannot clone
Gitea. The snapshot was removed at the fork.

## HA tool names

The HA Add-on Store includes Headscale alongside Odoo, n8n, Hermes, EMQX,
LiteLLM and Nextcloud. OpenDesign is a source candidate; K8s and VK are absent.
HA uses its own GUI, HA authentication and ingress adapter. These are genuine
distribution differences; the Podman workspace keeps the PaaS product contracts
of the fork base and does not claim that HA already publishes all nine products.

At HA source commit `1b45bdfa5338170d2b397ef5940bb4c241f8b343`, the
six published products have exactly the same tool names as PaaS/Podman:
EMQX 39, Hermes 11, n8n 28, Odoo 41, LiteLLM 40, and Nextcloud 9.
The unpublished OpenDesign source also matches at 15. The Headscale source
has 11 matching tools; PaaS/Podman additionally expose the optional
`headscale_platform` integration tool. CI checks these name sets against the
pinned HA checkout using `scripts/check-ha-tool-parity.py`. This check is a
tool-name contract, not a claim that HA's GUI, authentication or unconfigured
backend operations were tested through Podman.
