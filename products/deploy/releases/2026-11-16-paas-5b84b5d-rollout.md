# PaaS 5b84b5d product rollout (2026-11-16)

Status: **released; all nine products run it on the owner's Podman host.**
This is the second rollout in `docs/plans/2026-11-14-multi-profile-alignment.md`
(refresh 1c, decision 8). The first one is
`products/deploy/releases/2026-11-14-multi-profile-rollout.md`.

## Source

- Podman `main` `d27cef619c98168bb7d7686d495f0118a086cb91`, which merges #35.
- Canonical PaaS `5b84b5d0fc70ab96a580629f1377ab65159ea96d`. Since the first
  rollout's `5a06d53` it adds Gitea #149 and #151-#154:
  - #149: query credentials are masked in logs.
  - #151: VK is removed.
  - #152 and #154: Nextcloud's connection URL passes the SSRF check, and a new
    user or URL replaces the MCP token.
  - #153: EMQX fixes.
- Product images are `paas-5b84b5d-podman.1`; Headscale is `0.1.6-podman.1`.
- All three GitHub workflows passed on `d27cef6`.

## Release decision

As in the first rollout, the owner released without a signed manifest. Every
upgrade ran with `--skip-release-signature` from a clean clone whose HEAD was
`d27cef6` on `origin/main`. Each image carried that commit, the catalog pin
and a build ID in its labels, and the installer pinned the image ID under its
lock.

## Images

`products/deploy/build-release-images.sh` built the images one at a time from
that clone. The build IDs are `local-d27cef619c98-<UTC time>`. The previous
images, from the first rollout, stay on the host for rollback.

| Product | Release image ID | Previous image ID |
|---|---|---|
| headscale | `a91c7219c9cae114874df471eadb96f43c5daeafaa03f1ff65909fc0a672e2f9` | `7efa98d1649d577ebe20c3f98162e784a60f9df21d1a4d89804e48c14b24c49a` |
| emqx | `aeebf9d3cef302b22849ff3bf275816a01eab8517232c6022a8a077bce0dd18b` | `48ef8dad194f82973b32e7ca49093d832a620fba2a70320e3450c0eec1e8f5e6` |
| hermes | `9b9873f9a71f6c80cadea313cbd4c2191c23bdc25a14441e4469c4845c9f1ce1` | `058e40bcb032ee9a42db1aa0e6bf298aa48454e534b76c410dc0b469d1a46f05` |
| n8n | `8ccf2811d6ab1eca97b35bc8fa19e164787b1ee5c3b800fa948656ab77343c14` | `a2851bff4e965f5d00d7f7cce519d3aada1999ad23fdfde4e6206edea4227562` |
| odoo | `66eda7e05808b0f6e356b7c8dd3ee6dad04d66a14794a632628a1cbec3559f9e` | `97108907737a3c7868d62bda0c6a70c08a46e0068e801c212686da12cb59de5e` |
| litellm | `c616b1a84ba3e297bc78c3b3a94d2236170726a9aff6be2a28c8ac618e38c51a` | `0765f0704e2b7f1fc3538908bb4b18a3085e07b76cb19d29d5d3e18ffb541d5c` |
| nextcloud | `79c7e9448057cd831012f223f38cb2a776d132819825e5f89a43b6142ddc7c65` | `dea5a6acfa09926a33e9d745c4705e2fc020cf4b4a61b6ed09fbf58938ad2ecb` |
| opendesign | `ca129b935c2c8ebf7b1b1670d1f3d9f69da4d087b5f5d3a4f3de19c8f4ce6b6c` | `df1fdeb1827c67b9d2d2f65b2c2c40d976655049c0928665fd81252693d4a4ce` |
| k8s | `c577c32ff2d6841f289ef28f3e16e9c18c29f8ced131b0ab18fc476bc1138da7` | `81b6c6a964c878d416bb60d7ec40d51ced82217aa2edff42934aed6bfb8fb63b` |

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

Before #35 merged, the backends deployed on the same host were tested with two
profiles. Each tool was called through each profile's own MCP URL.

These test images were built from #35's sync commit `6291713`. Its `products/`
tree differs from `d27cef6` only in `products/README.md`, and no image copies
that file. The results therefore apply to the release images.

| Product | Calls | Checks | Also verified |
|---|---|---|---|
| Nextcloud | 35/35 | 41/41 | A private URL is refused in strict SSRF mode. A new user or URL replaces the MCP token. An app password change, or the same URL written differently, keeps it. |
| EMQX | 75/75 | 29/29 | A 400,000-character identifier returns `EMQX_DESTINATION_DENIED` with no traceback in the output. |
| Odoo | 83/83 | 25/25 | |
| Headscale | 58/58 | 31/31 | The deployed policy was unchanged. |
| OpenDesign | 25/25 | 21/21 | |
| LiteLLM | 64/64 | 21/21 | The free model was rate-limited upstream. Both profiles still got a clean error, and profile A completed a chat on another model. |
| Hermes | 48/49 | 32/33 | The one failure is the known cron issue in draft #24 (a job created disabled comes out enabled), which is not upstream yet. |

- **Hermes chat through profile B.** The first try hit an expired LLM provider
  login on the backend. A separate retry passed once the login had refreshed.
- **Credentials in logs.** For every product, no credential the test used
  appeared in the container output or on any profile's Logs page.
- **Not covered.** n8n and K8s had no real-backend test this round: n8n is
  waiting for a test API key, and the host has no Kubernetes cluster.

## Configuration change: Nextcloud SSRF mode

Since PaaS #152, the Nextcloud admin checks its Nextcloud URL against the core
SSRF rules. It does this when the connection is saved, on Test connection and in
the health probe.

The installed Nextcloud admin had no `WEBHOOK_SECURITY_MODE`, so it ran in strict
mode, and it reaches a Nextcloud on the same host. A throwaway upgrade of such a
connection showed what strict mode does:

- the MCP tools and the MCP URL kept working;
- Test connection and the health probe reported an SSRF block;
- saving the connection again returned 400.

The owner chose permissive mode, the same mode the Hermes admin runs with. It
still refuses cloud metadata addresses. `WEBHOOK_SECURITY_MODE=permissive` was
added to the Nextcloud admin's env file before its upgrade, and the upgrade
reuses that file. It is the only line that changed.

## Rollout

| When (UTC) | What |
|---|---|
| 2026-11-15 22:51-22:54 | opendesign (canary), emqx, hermes, n8n, odoo, litellm, k8s and headscale, one at a time |
| 2026-11-15 23:33 | nextcloud, after the owner chose the SSRF mode |

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

All nine passed, and no rollback ran.

The MCP state of each product is the same as before the rollout:

- EMQX has no saved backend connection.
- K8s has no cluster configured.
- n8n has no API key saved, so its MCP child runs in documentation mode.
- The other six products run their MCP child against their backend.

## Rollback

Each upgrade printed two locations:

- a unit snapshot, in `~/.local/state/woow-quadlet/<app>/rollback/`;
- a volume backup taken just before it, in `~/backups/<app>/`.

To go back to the previous image, follow "Rollback" in the 2026-11-14 record.

To put Nextcloud's SSRF mode back as well, remove the `WEBHOOK_SECURITY_MODE`
line from its env file. The file is then exactly as it was before.
