# Podman fixes rollout (2026-11-18)

Status: **released; all nine products run it on the owner's Podman host.**
This is the fourth rollout in `docs/plans/2026-11-14-multi-profile-alignment.md`.
The previous one is `products/deploy/releases/2026-11-17-podman-owned-rollout.md`.

## Source

- Podman `main` `a63ba8574a8d329877fc0c68bc1c19f2330ce62e`. Since the previous
  rollout it merges:
  - #40: the previous release record (documents only);
  - #41: three fixes found by end-to-end tests of the Podman products:
    - K8s: when `HOME` is not writable, the launcher points any unset
      `HELM_CACHE_HOME`, `HELM_CONFIG_HOME` and `HELM_DATA_HOME` at the
      temporary directory, so `helm_install`, `helm_list` and `helm_uninstall`
      work in the read-only container.
    - Open Design: a blank `OD_CHAT_TIMEOUT` no longer makes the child exit with
      code 1. New profiles start with blank connection values, and the
      Connection page treats a blank value as "clear".
    - Setup hints: a deployment can set `MCP_ADMIN_URL`, and the Quadlet
      templates set it to the local admin address. MCP clients that arrive
      through a host name without the admin page are pointed at the page that
      exists.
- The PaaS fork base is unchanged: `48f394d6fbce3c38a22b75f06e51ec208f230d91`.
- Product images are `paas-48f394d-podman.3`; Headscale is `0.1.7-podman.2`.
- All 14 GitHub checks passed on #41's head `dd84db1`. `a63ba85` has the same
  tree as `dd84db1`, because `main` did not move while #41 was open.

## Release decision

The owner merged #41 and then approved this release and rollout. As in the
previous rollouts, the release is unsigned. Every upgrade ran with
`--skip-release-signature` from a clean clone whose HEAD was `a63ba85` on
`origin/main`. Each image carries that commit, the fork base and a build ID in
its labels, and the installer pinned the image ID under its lock.

## Images

`products/deploy/build-release-images.sh` built the images one at a time from
that clone. The build IDs are `local-a63ba8574a8d-<UTC time>`, from
`20261118T040948Z` (headscale) to `20261118T041306Z` (k8s). The previous
images, from the third rollout, stay on the host for rollback.

| Product | Release image ID | Previous image ID |
|---|---|---|
| headscale | `a9137408e116a5d04e2125cec52ae9281ad80d9f46c18c88e1d19a0dadee7fd1` | `85c764b4ace986adbbf280a45de4cb20cbea26cfa728f2ca0a831761baf15515` |
| emqx | `f2417c4508bcc2db5c8d184dc46760a81c36652f1cbb74f06a6da898b706f0ce` | `8731deb1000b11ed8dea6506b664492e5240599d905c18fe78a74d4ceb7265b6` |
| hermes | `3c7423a94aa04f6ac5b2ccb1c691bef376571aac04c22f41ab3ddd77bfcb8bd9` | `4909e8eef5c40fe59894daf17f6157a4b266b086ed9c43c46469ce034a1310b3` |
| n8n | `8c665b8366e16e6dd1cc7b78283630f3eb96d288af7c5ec5ccd4ed5015f10517` | `9b4fb61177885b24a641e9e8d442986b0295029f525bb8833dfdc43a9fad03f2` |
| odoo | `fed3188ac80268bddb22cd819b279be086ce211dc03d84e2f7d94aaef950b0cb` | `47fe255c96f6093e13c692d602df34a7e6f279653a998d97b5657d9d26acb02e` |
| litellm | `0e54c6c21b261a815252bc5053537382dcabe4d40eafc18de58e4cba0dbfd43a` | `b33c384046eb1be451ea00002e4c952451a8ac0f80b37549567da17dda0f0fd6` |
| nextcloud | `dc65820917e80dafed6cdf523ad49cb41af3c96cb91aae2b60d792b0636bd0b5` | `ce14e6df978d5ef8bdff7eb33b4f98a52e673853d89d2e40f7195ed8fb0ba722` |
| opendesign | `3a5033503e4b7a364477b9f897f59497f6ee23bb71d4376b8c8743e079e25207` | `aba95490a38d3ae267df4a83bea7c7458c43742fdd9438810e1b0a4b367c0e11` |
| k8s | `4e0c053e23a218a0050dc3116cb846c42646242fef8215ef3bad868b60855d9a` | `066d96ef0823c7f956857a013b33b9573ab2ffea45d85a7946486e1789cba8e7` |

## Checks before the rollout

Every release image passed the same three checks as in the previous rollout,
on throwaway stacks with generated credentials:

1. **Release check.** `release_preflight.py --unsigned` accepted the image: 9/9.
2. **Container smoke.** First boot, GUI, MCP, a second profile, restart
   persistence and the revoked restore: 9/9.
3. **Upgrade from the installed image.** The installed image wrote the data,
   then the release image started on the same volume. The GUI password, MCP
   token, owner-defaults marker and tool policy were unchanged, a second profile
   could be added, and a restart left `config.json` unchanged: 9/9.

The fixes in #41 came out of end-to-end tests that call every tool. These ran
on throwaway stacks of the third-rollout images, with a Default and a second
profile on the same backend:

| Product | Tools | Default | Second profile |
|---|---|---|---|
| EMQX | 39 | 48/48 | 48/48 |
| n8n | 28 | 43/43 | 43/43 |
| K8s (with the Helm directories) | 34 | 43/43 | 43/43 |

The Default and the second profile gave the same result for every call. Some
tools reported a missing feature of the test environment rather than a fault:
no OpenShift, kcp or Tekton on the cluster, no instance-level MCP access in
n8n at the time, and an n8n license without folders.

## Rollout

| When (UTC) | What |
|---|---|
| 2026-11-18 04:16:52-04:17:05 | opendesign (canary) |
| 2026-11-18 04:18:18-04:20:20 | emqx, hermes, n8n, odoo, litellm, nextcloud, k8s and headscale, one at a time |

Each product ran `products/deploy/manage.sh PRODUCT upgrade --skip-release-signature`.
Each upgrade:

- took a unit snapshot and a volume backup;
- installed the release image;
- waited for health;
- ran the read-only smoke.

After each upgrade:

- the container ran the release image ID, and the Quadlet named it;
- the unit was active and healthy;
- `memory.max` was 1 GiB;
- the unit carried `MCP_ADMIN_URL` with the product's local admin address.

All nine passed, and no rollback ran.

## After the rollout

- The MCP state of each product is the same as just before the rollout: all
  nine run their MCP child.
  - EMQX and K8s now have a saved backend connection; the owner asked for it on
    2026-11-18, before the rollout.
  - n8n also has an instance-level MCP access token.
- Env files did not change:
  - K8s keeps the three `HELM_*_HOME` lines added the same day as a stopgap
    before this release. They match what the launcher now sets by itself and
    can stay.
  - Nextcloud keeps the permissive SSRF mode.
- The fixes, checked on the live services:
  - **Open Design**: a new profile with blank connection values started its
    child ("Open Design MCP Server") and kept it running, and no unexpected
    exit was logged. The profile was then deleted.
  - **Setup hints**: a new EMQX profile without a connection, reached through
    the URL that external MCP clients use, answered with the local admin page
    and did not name the external host. The profile was then deleted.
  - **K8s**: `helm_install`, `helm_list` and `helm_uninstall` passed on the
    Default profile.
- Ten minutes after the rollout, all nine were healthy and none had restarted.

## Rollback

Each upgrade printed two locations, named `20261118-1216..1219`:

- a unit snapshot, in `~/.local/state/woow-quadlet/<app>/rollback/`;
- a volume backup taken just before it, in `~/backups/<app>/`.

To go back to the previous image, follow "Rollback" in the 2026-11-14 record.
