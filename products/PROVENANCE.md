# MCP product source and parity

The ten active `products/apps/*` and `products/packages/mcp-admin-core` and
`mcp-admin-ui` trees come from `WOOWTECH/woow-mcp-server` `main` commit
`c077be987d47d80240ee9de1810a91f6646845e2` (GitHub). The Headscale app
implements the Headscale v0.29.4 29-operation native contract, its dedicated
management GUI, operation policy, Origin checks, session revocation, and the
optional PaaS binding. The HA Add-on's Headscale child came from the same
source family at `d7c78aaeff001f5fcf87f69ecd3db290396a5906`, with HA-specific
authorization and ingress adaptation.

For the Headscale application/core code, the only source edit is
`deployment_kind="podman"` in `apps/headscale/headscale_mcp_admin/main.py`.
For the other nine products, application and UI source is identical except:

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

The HA Add-on repository publishes six products at its current release:
Odoo, n8n, Hermes, EMQX, LiteLLM and Nextcloud. It has OpenDesign and Headscale
source candidates but those are not part of that published six-product set;
K8s and VK are absent. HA also uses its own GUI, HA authentication and ingress
adapter. These are genuine upstream distribution differences; the Podman
workspace follows the pinned PaaS product contracts, and it does not claim
that the HA distribution already publishes all ten products.
