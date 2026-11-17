# Podman-owned product rollout (2026-11-17)

Status: **released; all nine products run it on the owner's Podman host.**
This is the third rollout in `docs/plans/2026-11-14-multi-profile-alignment.md`,
and the first since this repository took over the product source (decision 10).
The previous one is `products/deploy/releases/2026-11-16-paas-5b84b5d-rollout.md`.

## Source

- Podman `main` `07fec87126f174b000ce358a0d297a3b3648f812`. Since the previous
  rollout it merges:
  - #37: the last refresh from PaaS, to `48f394d`;
  - #38: this repository owns the product source from now on;
  - #39: the Hermes cron change, the first one made here.
- The PaaS fork base `48f394d6fbce3c38a22b75f06e51ec208f230d91`. Since the
  previous rollout's `5b84b5d` it adds Gitea #155, #135, #139, #138 and #156:
  - #155: request bodies over 64 KiB count against a gateway-wide and a
    per-profile byte budget, uploads have idle and total deadlines, a JSON
    structure guard runs before parsing, and non-UTF-8 bodies are refused.
  - #135 and #139: EMQX and LiteLLM keep no idle connections, so the first
    call after a network recovery does not wait on a stale one.
  - #138 and #156: a Hermes cron job created with `enabled=false` is created
    paused, and the tool description says so.
- #39: a Hermes cron job created with `enabled=false` is checked against the
  state Hermes saved and paused if it still came out enabled, and `update`
  takes `enabled`.
- Product images are `paas-48f394d-podman.2`; Headscale is `0.1.7-podman.1`.
- All three GitHub workflows passed on `07fec87`.

Images built from #37 alone (`paas-48f394d-podman.1`) passed the same checks
on 2026-11-16 but were never rolled out. They have been deleted.

## Release decision

As in the previous rollouts, the owner released without a signed manifest.
Every upgrade ran with `--skip-release-signature` from a clean clone whose HEAD
was `07fec87` on `origin/main`. Each image carried that commit, the fork base
and a build ID in its labels, and the installer pinned the image ID under its
lock. The owner approved this rollout in advance, on the condition that every
check below passed.

## Images

`products/deploy/build-release-images.sh` built the images one at a time from
that clone. The build IDs are `local-07fec87126f1-<UTC time>`. The previous
images, from the second rollout, stay on the host for rollback.

| Product | Release image ID | Previous image ID |
|---|---|---|
| headscale | `85c764b4ace986adbbf280a45de4cb20cbea26cfa728f2ca0a831761baf15515` | `a91c7219c9cae114874df471eadb96f43c5daeafaa03f1ff65909fc0a672e2f9` |
| emqx | `8731deb1000b11ed8dea6506b664492e5240599d905c18fe78a74d4ceb7265b6` | `aeebf9d3cef302b22849ff3bf275816a01eab8517232c6022a8a077bce0dd18b` |
| hermes | `4909e8eef5c40fe59894daf17f6157a4b266b086ed9c43c46469ce034a1310b3` | `9b9873f9a71f6c80cadea313cbd4c2191c23bdc25a14441e4469c4845c9f1ce1` |
| n8n | `9b4fb61177885b24a641e9e8d442986b0295029f525bb8833dfdc43a9fad03f2` | `8ccf2811d6ab1eca97b35bc8fa19e164787b1ee5c3b800fa948656ab77343c14` |
| odoo | `47fe255c96f6093e13c692d602df34a7e6f279653a998d97b5657d9d26acb02e` | `66eda7e05808b0f6e356b7c8dd3ee6dad04d66a14794a632628a1cbec3559f9e` |
| litellm | `b33c384046eb1be451ea00002e4c952451a8ac0f80b37549567da17dda0f0fd6` | `c616b1a84ba3e297bc78c3b3a94d2236170726a9aff6be2a28c8ac618e38c51a` |
| nextcloud | `ce14e6df978d5ef8bdff7eb33b4f98a52e673853d89d2e40f7195ed8fb0ba722` | `79c7e9448057cd831012f223f38cb2a776d132819825e5f89a43b6142ddc7c65` |
| opendesign | `aba95490a38d3ae267df4a83bea7c7458c43742fdd9438810e1b0a4b367c0e11` | `ca129b935c2c8ebf7b1b1670d1f3d9f69da4d087b5f5d3a4f3de19c8f4ce6b6c` |
| k8s | `066d96ef0823c7f956857a013b33b9573ab2ffea45d85a7946486e1789cba8e7` | `c577c32ff2d6841f289ef28f3e16e9c18c29f8ced131b0ab18fc476bc1138da7` |

## Checks before the rollout

Every release image passed these checks on throwaway stacks with generated
credentials:

1. **Release check.** `release_preflight.py --unsigned` accepted the image.
2. **Container smoke.** The run covers first boot, GUI, MCP, a second profile,
   restart persistence and the revoked restore.
3. **Upgrade from the installed image.** The installed image wrote the data, then
   the release image started on the same volume. Afterwards:
   - the GUI password, MCP token, owner-defaults marker and tool policy were
     unchanged;
   - a second profile could be added;
   - a restart left `config.json` unchanged.

Before #37 merged, the backends deployed on the same host were tested with two
profiles. Each tool was called through each profile's own MCP URL. The test
images were built from #37's sync commit `da99ae1`. Between that commit and
`07fec87`, the only file any image copies that changed is under
`apps/hermes`, in the Hermes image; the other changes are documents, checks and
the catalog's image version.

| Product | Calls | Checks | Also verified |
|---|---|---|---|
| Nextcloud | 35/35 | 41/41 | |
| EMQX | 75/75 | 29/29 | An over-long identifier left no traceback in the container output. |
| Odoo | 83/83 | 25/25 | |
| Headscale | 58/58 | 31/31 | The deployed policy was unchanged. |
| OpenDesign | 25/25 | 21/21 | |
| LiteLLM | 64/64 | 21/21 | |
| Hermes | 49/49 | 33/33 | A job created with `enabled=false` is listed as disabled. |
| n8n | 66/66 | 30/30 | |

- **Request-bytes budget.** In a 1 GiB container, each of the eight images
  computed a 16 MiB gateway-wide and per-profile budget.
- **Hermes with #39.** The image built from #39's head passed the cron check
  16/16 and the full Hermes run with 49/49 calls and 33/33 checks. The cron
  check covers `update` with `enabled` alone, with a new name and with a new
  schedule.
- **Hermes release image.** After the rollout, the cron check passed 16/16 on
  the release image itself, in a separate test admin, so the live Hermes
  admin was not changed.
- **Credentials in logs.** For every product, no credential the test used
  appeared in the container output or on any profile's Logs page. No test
  object was left on a deployed backend.
- **Not covered.** The host has no Kubernetes cluster, so K8s had only the
  container checks.

## Rollout

| When (UTC) | What |
|---|---|
| 2026-11-17 01:12 | opendesign (canary) |
| 2026-11-17 01:13-01:16 | emqx, hermes, n8n, odoo, litellm, nextcloud, k8s and headscale, one at a time |

Each product ran `products/deploy/manage.sh PRODUCT upgrade --skip-release-signature`.
Headscale goes through its own `upgrade.sh`. Each upgrade did the following:

- took a unit snapshot and a volume backup;
- installed the release image;
- waited for health;
- ran the read-only smoke.

After each upgrade:

- the container ran the release image ID, and the Quadlet named it;
- the unit was active and healthy;
- `memory.max` was 1 GiB.

All nine passed, and no rollback ran. The MCP state of each product is the same
as before the rollout: EMQX has no saved backend connection, K8s has no cluster
configured, and the other seven products run their MCP child. Nextcloud keeps
the permissive SSRF mode set in the previous rollout; its env file did not
change. Ten minutes after the rollout, all nine
were healthy and none had restarted.

## Rollback

Each upgrade printed two locations:

- a unit snapshot, in `~/.local/state/woow-quadlet/<app>/rollback/`;
- a volume backup taken just before it, in `~/backups/<app>/`.

To go back to the previous image, follow "Rollback" in the 2026-11-14 record.
