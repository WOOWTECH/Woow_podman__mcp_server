# Multi-profile product rollout (2026-11-14)

Status: **released; all nine products run it on the owner's Podman host.**
This is the Phase 3 record of `docs/plans/2026-11-14-multi-profile-alignment.md`.
It replaces the 2026-11-12 admin-session rollout plan in draft #17.

## Source

- Podman `main` `bd6ba26d90f6d1af17935b7a93033e6098fe9dc4`, with #26, #31 and #32 merged.
- Canonical PaaS `5a06d533eb433ad511bba8e2db946a0de922925d`. Product images
  are `paas-5a06d53-podman.1`; Headscale is `0.1.5-podman.1`.
- All five GitHub workflows passed on `bd6ba26`.

## Release decision

The owner released without a signed manifest. What that keeps and what it
gives up is in "Owner-approved release without a signature" in
`docs/release-provenance-guard.md`. Every upgrade ran with
`--skip-release-signature` from a clean clone whose HEAD was on `origin/main`.
Each image carried that commit, the catalog pin and a build ID in its labels,
and the installer pinned the image ID under its lock.

## Images

`products/deploy/build-release-images.sh` built the images one at a time from
that clone. The build IDs are `local-bd6ba26d90f6-<UTC time>`. The previous
images stay on the host for rollback.

| Product | Release image ID | Previous image ID |
|---|---|---|
| headscale | `7efa98d1649d577ebe20c3f98162e784a60f9df21d1a4d89804e48c14b24c49a` | `c6811a6f663f8e49fd5abf47f2248a2d730b943bf7d261851b373e11763c217f` |
| emqx | `48ef8dad194f82973b32e7ca49093d832a620fba2a70320e3450c0eec1e8f5e6` | `fa3bab9eedee441a71b3e7530928091eb3574ea5d6b88c6d1aef603d770cdb5f` |
| hermes | `058e40bcb032ee9a42db1aa0e6bf298aa48454e534b76c410dc0b469d1a46f05` | `06540c90a5045d20adfd9eaf22fe39cac85d6d00ac8c8e7b3b4a026115cb4f59` |
| n8n | `a2851bff4e965f5d00d7f7cce519d3aada1999ad23fdfde4e6206edea4227562` | `64fe5be23d80d55aed744b8148a9212fcbd8a58fb56b53b6bf313dd3fd721cc8` |
| odoo | `97108907737a3c7868d62bda0c6a70c08a46e0068e801c212686da12cb59de5e` | `15add7bc16784faf8d01e56854f2a314f2f8f716c543ec83b7233b82c901a53d` |
| litellm | `0765f0704e2b7f1fc3538908bb4b18a3085e07b76cb19d29d5d3e18ffb541d5c` | `db88c53fb142dae479b5c63f2e6f8fe0b947d0f8d815fe73b2ab36dce6faf5a0` |
| nextcloud | `dea5a6acfa09926a33e9d745c4705e2fc020cf4b4a61b6ed09fbf58938ad2ecb` | `3f6895311687e60c9a73a0e26691b2b0a0f2915ad96268bcedb7bb1065bf7d00` |
| opendesign | `df1fdeb1827c67b9d2d2f65b2c2c40d976655049c0928665fd81252693d4a4ce` | `01c2001f592a9de5d094f7b7ec40253432a54ae769675ded200f4f3eff25d150` |
| k8s | `81b6c6a964c878d416bb60d7ec40d51ced82217aa2edff42934aed6bfb8fb63b` | `e6c60d60fc92a933dd03cc4340add36d69f1d6ce284b09682549b3b3abfd462f` |

## Checks before the rollout

Every product passed these checks on throwaway stacks with generated
credentials:

1. **Release check.** `release_preflight.py --unsigned` accepted the image.
2. **Container smoke.** This is `products/deploy/shared/tests/container-smoke.sh`,
   or Headscale's own script. It covers first boot, GUI, MCP, a second
   profile, restart persistence and the revoked restore.
3. **Upgrade from the installed image.** The previous image wrote the data,
   then the release image started on the same volume. Afterwards:
   - the GUI password, MCP token, `owner_defaults_20261025` marker and tool
     policy were unchanged;
   - a second profile could be added (Headscale kept the one it already had);
   - a restart left `config.json` unchanged.

Two checks failed once while the host's load average was 20-30, and both
passed when rerun:

- the Headscale upgrade test;
- the k8s container smoke, where tool discovery took longer than 15 seconds.

CI passed both on `bd6ba26`.

A GUI login made on the previous image gets HTTP 401 after the upgrade, and a
new login works. Admins therefore sign in once more. MCP token URLs keep
working.

## Rollout

The rollout ran on 2026-11-14 from 10:52 to 10:56 UTC, one product at a time:
opendesign (canary), emqx, hermes, n8n, odoo, litellm, nextcloud, k8s,
headscale.

Each product ran `products/deploy/manage.sh PRODUCT upgrade --skip-release-signature`.
Headscale goes through its own `upgrade.sh`. The upgrade did the following:

- took a unit snapshot and a volume backup;
- installed the release image;
- waited for health;
- ran the read-only smoke.

After each upgrade:

- the container ran the release image ID, and the Quadlet named it;
- the unit was active and healthy;
- `memory.max` was 1 GiB.

All nine passed, and no rollback ran.

State afterwards:

- The MCP child runs for opendesign, hermes, n8n, odoo, litellm, nextcloud
  and headscale.
- emqx has no saved backend connection (`not_configured`).
- k8s has no cluster configured (`not_running`): its child exits and the core
  retries with backoff. The previous image behaved the same way on such data.
- Each container uses about 45-160 MB.

## Rollback

`upgrade` rolls back by itself when the install or its smoke fails. Each
upgrade printed two locations:

- a unit snapshot, in `~/.local/state/woow-quadlet/<app>/rollback/`;
- a volume backup taken just before it, in `~/backups/<app>/`.

To return to the previous image later, copy the snapshot's unit files and
manifest back the way the automatic rollback in
`products/deploy/shared/lifecycle.sh` (Headscale: `scripts/upgrade.sh`) does,
then run `systemctl --user daemon-reload` and restart the unit.
`manage.sh PRODUCT restore BACKUP_DIR` also brings the volume back, but it
revokes every MCP token in it by design.
