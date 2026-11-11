# Seven-product admin session revocation rollout

Status: **canonical source release blocked; seven shared Quadlets unchanged**.
Podman PR #16 is in `main` at `acc3de6ac0ace35b0ad31ccae3043bcbdeb55aaf`,
after Headscale PR #15; parity PR #18 is also in `main` at `fe4f0b4`.
This plan covers EMQX, Hermes, n8n, Odoo, LiteLLM, Nextcloud, and OpenDesign.
Headscale uses a separate image, installer, and session policy; release it through
its own owner and checks.

## Release gate

`products/deploy/catalog.json` still pins canonical PaaS commit
`49c9a66e95e94ba9ab8630fc2a86488d1c649b2e` and image version
`paas-49c9a66-podman.1`. `products/PROVENANCE.md` and
`products/scripts/check-all-parity.py` require the vendored app, shared core,
tests, and UI to match that exact source commit, with only documented Podman
transformations. The session and Headscale DNS fixes in Podman `main` are absent
from this pin. The
GitHub PaaS mirror PR #4 is a draft and is not a canonical Gitea release.
The local source mirror contains the pinned commit and its content was audited;
access to Gitea itself redirects to Cloudflare Access, so canonical merge
status and the final source release SHA cannot be verified here.
The five GitHub workflows on Podman merge commit `acc3de6` succeeded, but
`all-mcp-products` does **not** run `check-all-parity.py` against Gitea. This
is a documented/manual provenance gate, not a GitHub branch protection rule.

Current seven shared Quadlets use `paas-c077be9-podman.1`. A source and runtime content
audit corrected an earlier version-order claim: canonical `c077be987d47d80240ee9de1810a91f6646845e2`
is an ancestor of `49c9a66e95e94ba9ab8630fc2a86488d1c649b2e`. Across
`apps/` and `packages/`, the latter changes 66 paths and deletes none. Runtime
modules extracted from **all seven live images** match `c077be9` exactly except
each product's documented Podman `main.py` deployment marker. Their shared
core lacks `profiles.py`; both the 49 source and all seven logout-fixed
candidate images contain it. Each candidate's runtime module content matches
current Podman `main` byte for byte. Thus there is **no identified c077 runtime
feature lost by moving to 49**; 49 adds the multi-profile implementation. The
private, redacted file-by-file SHA-256 receipt is
`work/mcp-unification-20261111/acceptance/podman-source-content-audit-20261112.json`.
Compiled UI bundles and third-party dependency layers were not byte-compared;
the approved release still needs fresh builds and GUI/backend smoke checks.

The real gate is release provenance and image identity. The catalog's 49 pin
does not contain the unmerged canonical logout/DNS changes, while Podman
`main` already does. No local image currently uses tag `paas-49c9a66-podman.1`.
`manage.sh install` would build the current Podman tree under that 49-labelled
tag if it is missing, or reuse whatever image carries the tag if present.
Neither case establishes an approved canonical-source image. Seven local
`logout-candidate` images are isolation artifacts, not released images.
**Do not deploy them or run `upgrade` until the source and image gates pass.**
The current installer does not fail closed on source parity or verify an
existing image's content identity. An independently reviewed installer guard
must enforce the exact vendored source pin and image digest/labels before any
Quadlet or persistent configuration change. Do not infer provenance from a tag.

1. Review and merge both Gitea source PR #132 (Headscale DNS) and #133 (shared
   session revocation); obtain the resulting exact canonical full commit SHA.
   Recheck that the merged tree includes the c077 baseline, the 49 multi-profile
   source, and both fixes by actual path/content comparison.
2. Refresh the Podman vendored source from that commit. Update `catalog.json`
   `paas_source_commit`, a **new unique** `image_version`, and `PROVENANCE.md`
   together. Preserve the seven current live image tags and IDs for rollback.
3. Run `check-all-parity.py` against a checkout containing the canonical Gitea
   commit. Keep the command output and the source SHA as release evidence.
4. Review and merge a fail-closed installer provenance guard. Confirm in
   isolation that stale/missing source proof, unknown image identity and an
   occupied tag cannot replace an active product.
5. Pass the full Podman CI, seven image builds and container smoke tests, and
   the rootless Quadlet dry-run. Review the new image version before merge.
6. Build/tag seven release images from the approved source and record their
   immutable image IDs. Deploy only through `products/deploy/manage.sh`.

Headscale PR #15 is also merged. Its live Quadlet now uses
`localhost/woow-headscale-mcp-admin:0.1.2-podman.1`; its dedicated source
provenance and live backend checks remain separate from this seven-product rollout.

The current host inventory, including live image IDs, health and persistent
volume checks, is in the private acceptance file
`work/mcp-unification-20261111/acceptance/podman-logout-live-inventory-20261112.json`.

## Per-product deployment and validation

Perform these steps serially, one product at a time. The seven rootless
Quadlet services have a dedicated named `/data` volume, a mode 0600 env file,
and persistent Podman secrets. Their current live images remain in local image
storage. Use the existing host owner; its user systemd bus requires:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path="$XDG_RUNTIME_DIR/bus"
```

For each product, record the exact old image ID, unit file SHA256, volume name,
and health. Take a private `manage.sh PRODUCT backup` and keep its returned
directory and SHA256. Before the upgrade, log into the **live** API twice and
confirm both independent sessions can read `/api/settings` (HTTP 200). Keep
the JWTs only in process memory; record hashes and HTTP codes, never tokens or
cookies. A backup stops and restarts the service, so confirm both sessions
still work after backup and before upgrade.

Run `manage.sh PRODUCT upgrade` from the approved release checkout. It saves a
Quadlet manifest snapshot, backs up the volume, builds the new unique image if
missing, and starts the service. Record the snapshot and backup directories.
Check the running image ID equals the approved release image, user unit is
active, container is healthy, and `/healthz` is HTTP 200. Run `manage.sh
PRODUCT smoke`. Do a read-only saved-connection backend probe: Odoo,
LiteLLM, Nextcloud, and OpenDesign use `/api/config/test`; Hermes uses
`/api/config/test/gateway`. n8n and EMQX need a configured backend and a
read-only real backend call before their backend acceptance can pass; a
`setup_status` placeholder is insufficient.

Then prove session behavior against that **live upgraded container**:

- Replay a JWT issued by the old image as a bearer token and as a cookie;
  both must return 401. These legacy JWTs have no `jti` and must require login.
- Log in twice on the new image. Logout only the first session; expect 200.
  Replay its bearer and cookie separately; both must return 401. The second
  session must still return 200.
- Restart that Quadlet service. Replay the revoked first session again; both
  transports must still return 401. The independent second session must still
  return 200. Recheck health and the same read-only backend probe.

If any gate fails, mark that product **not accepted** and roll back before
moving to the next. Never put JWTs, cookies, passwords, backend credentials,
or raw configuration in the receipt. The isolation-only seven-image matrix is
`work/mcp-unification-20261111/acceptance/podman-logout-candidate-matrix-20261112-current-main.json`;
it does not count as live acceptance.

## Exact rollback on this host

`manage.sh upgrade` automatically restores the prior unit and volume if its
install step fails. A **post-upgrade validation failure** needs an explicit
rollback. Use the `SNAPSHOT` and `BACKUP_DIR` recorded for that product; do not
guess a timestamp. The snapshot contains the previous Quadlet files and
manifest, and the previous image tag is retained. The backup contains the
pre-upgrade volume and env file; same-host Podman secrets remain unchanged.

```bash
PRODUCT=emqx  # replace with the failed product
APP="$PRODUCT-mcp-admin"
STATE="$HOME/.local/state/woow-quadlet/$APP"
SNAPSHOT=/absolute/path/recorded/from/upgrade
BACKUP_DIR=/absolute/path/recorded/from/backup
systemctl --user stop "$APP.service"
if [[ -f $STATE/manifest ]]; then
  while read -r _ path; do
    grep -qF -- "  $path" "$SNAPSHOT/manifest" || rm -f -- "$path"
  done < "$STATE/manifest"
fi
while IFS= read -r -d '' saved; do
  target=${saved#"$SNAPSHOT/files"}
  mkdir -p -- "${target%/*}"
  cp -p -- "$saved" "$target"
done < <(find "$SNAPSHOT/files" -type f -print0)
cp -p -- "$SNAPSHOT/manifest" "$STATE/manifest"
rm -f -- "$STATE/pending-restart"
systemctl --user daemon-reload
products/deploy/manage.sh "$PRODUCT" restore "$BACKUP_DIR" --yes
products/deploy/manage.sh "$PRODUCT" smoke
```

After rollback, verify the running image ID equals the captured **old** ID,
the unit is active, `/healthz` is 200, and the read-only backend probe succeeds.
`restore` verifies the volume checksum and rotates the admin session generation,
so prior GUI sessions must sign in again. Preserve the failed release receipt
and backup privately. Do not remove the old image until acceptance is complete.
