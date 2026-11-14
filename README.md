# Woow Podman MCP Server

**English** · [繁體中文](README_zh-TW.md)

This repository holds a [nine-product MCP workspace](products/README.md) under
`products/`: Headscale, EMQX, Hermes, n8n, Odoo, LiteLLM, Nextcloud, OpenDesign
and K8s. The workspace shares the PaaS application/core/frontend source and
deploys each product with its own rootless Podman Quadlet, port, volume and
credentials. Product containers do not mount the Podman socket.

```bash
products/deploy/manage.sh list
products/deploy/manage.sh emqx install --dry-run \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
products/deploy/manage.sh emqx install \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
products/deploy/manage.sh emqx upgrade \
  --release-manifest /path/to/reviewed-release.json \
  --source-checkout /path/to/merged-canonical-checkout
```

Product install and upgrade require one of two things:

- an independently reviewed release manifest, clean source checkouts and an
  exact local image ID;
- for an owner-approved unsigned release, `--skip-release-signature` with an
  image that `products/deploy/build-release-images.sh` built from merged `main`.

See the [release proof and trust limits](docs/release-provenance-guard.md).
The paths above are placeholders. Product installers reject `--rebuild` and
require a prebuilt, reviewed image.

Headscale retains its [dedicated guide](products/deploy/headscale/README.md),
including its default `http://127.0.0.1:18081/` GUI, Origin settings and backups.

## Retired: the Podman host MCP

Until 2026-11-14 the repository root also held a Podman host MCP: the
`woow_podman_mcp_server` MCP server and its `podman_mcp_admin` console. They
exposed this host's Podman through the libpod API. The owner retired it, and
Pi Web now covers that work. Its last source is kept in the git tag
`archive/podman-host-mcp-20261114`.

`scripts/lib/quadlet-lib.sh` stays at the root because the product installers
source it. It is vendored from Woow_quadlet_migration_plan, and
`.github/workflows/tests.yml` checks it.

## Layout

```
products/                   the nine products: vendored PaaS source, deploy scripts, Quadlets
docs/                       plans and the release provenance guard
scripts/lib/                vendored quadlet-lib (sourced by products/deploy)
tests/                      quadlet-lib checks (lib-version.sh, lock-release.sh)
.github/workflows/          products CI, Headscale CI, quadlet-lib and credential checks
```
