# MCP product source and parity

The nine active `products/apps/*` and `products/packages/mcp-admin-core` and
`mcp-admin-ui` trees come from `WOOWTECH/woow-mcp-server` `main` commit
`0e432c3ebd7a481b8994105a187f29837d941fd6` (Gitea). The Headscale app
implements the Headscale v0.29.4 29-operation native contract, its dedicated
management GUI, operation policy, Origin checks, session revocation, and the
optional PaaS binding. The HA Add-on's Headscale child came from the same
source family at `d7c78aaeff001f5fcf87f69ecd3db290396a5906`, with HA-specific
authorization and ingress adaptation.

For the Headscale application/core code, the only source edit is
`deployment_kind="podman"` in `apps/headscale/headscale_mcp_admin/main.py`.
For the other eight products, application and UI source is identical except:

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
VK was retired from the Podman products on 2026-11-14. `apps/vk` stays
vendored byte for byte from PaaS, without the Podman edits, only so the uv
and npm workspace locks still match; no VK image is built or installed.

The existing root Podman-host MCP uses MCP SDK 1.x and remains in its own
environment. New MCP products use this `products/` workspace and the same
shared admin core as the PaaS source. The Headscale product resolves the
listed protocol/security dependency versions from the PaaS lock; the local
`uv.lock` records the full Podman product graph.

Run `python products/scripts/check-headscale-parity.py --source-checkout PATH`
and `python products/scripts/check-all-parity.py --source-checkout PATH` against
a checkout containing the pinned PaaS commit before refreshing the vendored
source. The latter checks every tracked application, core-test and frontend file
against the exact deployment-specific transformations listed above.
After both pass, run `python3 products/scripts/check-source-snapshot.py
--refresh-from PATH`. GitHub CI checks this reviewed snapshot because GitHub
Hosted Runners are redirected to Cloudflare Access when cloning Gitea.
The snapshot detects vendored-file and image-version drift in the Podman repo;
it does not independently prove that Gitea's live `main` still equals the pin.

The HA Add-on Store includes Headscale alongside Odoo, n8n, Hermes, EMQX,
LiteLLM and Nextcloud. OpenDesign is a source candidate; K8s and VK are absent.
HA uses its own GUI, HA authentication and ingress adapter. These are genuine
distribution differences; the Podman workspace follows the pinned PaaS product
contracts and does not claim that HA already publishes all nine products.

At HA source commit `1b45bdfa5338170d2b397ef5940bb4c241f8b343`, the
six published products have exactly the same tool names as PaaS/Podman:
EMQX 39, Hermes 11, n8n 28, Odoo 41, LiteLLM 40, and Nextcloud 9.
The unpublished OpenDesign source also matches at 15. The Headscale source
has 11 matching tools; PaaS/Podman additionally expose the optional
`headscale_platform` integration tool. CI checks these name sets against the
pinned HA checkout using `scripts/check-ha-tool-parity.py`. This check is a
tool-name contract, not a claim that HA's GUI, authentication or unconfigured
backend operations were tested through Podman.
