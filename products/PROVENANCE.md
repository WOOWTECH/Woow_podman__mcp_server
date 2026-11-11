# MCP product source and parity

The `products/apps/headscale` and `products/packages/mcp-admin-core` trees come
from `WOOWTECH/woow-mcp-server` `main` commit
`c077be987d47d80240ee9de1810a91f6646845e2` (Gitea). The Headscale app
implements the Headscale v0.29.4 29-operation native contract, its dedicated
management GUI, operation policy, Origin checks, session revocation, and the
optional PaaS binding. The HA Add-on's Headscale child came from the same
source family at `d7c78aaeff001f5fcf87f69ecd3db290396a5906`, with HA-specific
authorization and ingress adaptation.

The **only source edit** in the copied app/core trees is
`deployment_kind="podman"` in `apps/headscale/headscale_mcp_admin/main.py`.
Quadlet, first-boot credential seeding, and rootless volume ownership live in
`products/deploy/headscale` and do not change Headscale operations or GUI code.

The existing root Podman-host MCP uses MCP SDK 1.x and remains in its own
environment. New MCP products use this `products/` workspace and the same
shared admin core as the PaaS source. The Headscale product resolves the
listed protocol/security dependency versions from the PaaS lock; the local
`uv.lock` records the full Podman product graph.

Run `python products/scripts/check-headscale-parity.py --source-checkout PATH`
against a checkout containing that Gitea commit to detect divergence before
refreshing the vendored source.
