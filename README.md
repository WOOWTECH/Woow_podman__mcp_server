# Woow Podman MCP Server

**English** · [繁體中文](README_zh-TW.md)

A FastMCP server that exposes a **Podman host** through its libpod REST API as MCP tools, plus a
web admin console that supervises it, gates it, and publishes it on an authenticated URL that
Claude (or any MCP client) can connect to directly.

It runs as **one rootless Podman container that borrows the host's Podman socket**. The container
manages the host's containers by reaching the daemon over the bind-mounted socket directory; it
never runs a Podman of its own. Three components live in that one container:

| # | Component | What it is |
|---|-----------|------------|
| 1 | `woow_podman_mcp_server` | The MCP server. 23 tools over the libpod API, gated by a safety profile. Binds to loopback only. |
| 2 | `podman_mcp_admin` | The admin console: React SPA + FastAPI, on `:8080` inside the container. Supervises component 1 as a child process. |
| 3 | `mcp_admin_core` | Product-agnostic plumbing shared with the other Woow MCP consoles: app factory, JWT auth, config store, process manager, reverse proxy. |

The connector URL is `https://<host>/private_<mcp_auth_token>/mcp/`. The path segment **is** the
credential; see [Authentication](#authentication).

---

## Install (rootless Podman + Quadlet + systemd)

The supported deployment is a set of [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
units run by the user's systemd manager. Tested on Ubuntu 24.04 with podman 4.9.3 (the minimum
this repo accepts is 4.9) and systemd 255, rootless, with linger.

```bash
git clone https://github.com/WOOWTECH/Woow_podman__mcp_server.git
cd Woow_podman__mcp_server
scripts/install.sh                 # or: scripts/install.sh --port 18080
```

`scripts/install.sh` is idempotent. It:

1. checks the host (not root, podman >= 4.9, the Quadlet generator, a reachable
   `systemctl --user`), enables linger and `podman.socket` if they are off;
2. creates `~/.config/podman-mcp-admin/podman-mcp-admin.env` (mode 0600) from
   [`config/podman-mcp-admin.env.example`](config/podman-mcp-admin.env.example) on the first run
   and writes the daemon's API version into it. `--port N`, `--bind ADDR` and `--set KEY=VALUE`
   change a setting and save it there;
3. refuses to continue when a container named `podman-mcp-admin` exists that Quadlet does not
   manage, or when the old hand-written unit is still enabled (see
   [Migrating](#migrating-an-existing-deployment)), because Quadlet starts containers with
   `podman run --replace`;
4. renders the units in [`quadlet/`](quadlet/) with the env file (`@@VAR@@` tokens, whitelist in
   `quadlet/render-vars`) and checks them with the podman 4.9.3 generator and
   `systemd-analyze --user verify` before anything is installed;
5. builds `localhost/woow-podman-mcp-admin:<VERSION>` from the [`Dockerfile`](Dockerfile) when
   that tag does not exist yet (`--rebuild` forces it, `--no-build` forbids it);
6. creates three podman secrets if they are missing (random, never printed): the JWT signing key,
   the first-boot admin password and the first-boot connector token;
7. installs only the files that changed and restarts only the units whose files changed. A
   re-run with nothing changed restarts nothing;
8. waits for the container to be healthy and runs [`tests/smoke.sh`](tests/smoke.sh).

`scripts/install.sh --dry-run` renders and validates everything and reports what it would change,
without changing anything.

What gets installed:

| Path | What |
|---|---|
| `~/.config/containers/systemd/podman-mcp-admin.container` | the console; published on `127.0.0.1:8080` by default |
| `~/.config/containers/systemd/podman-mcp.volume` | volume `podman_mcp_data` (`/data/config.json`) |
| `~/.config/containers/systemd/podman-mcp.network` | network `podman-mcp` |
| `~/.config/podman-mcp-admin/podman-mcp-admin.env` | your per-host settings (0600) |
| podman secrets `podman-mcp-admin-{jwt-secret,password,mcp-token}` | `JWT_SECRET`, `ADMIN_PASSWORD`, `MCP_AUTH_TOKEN` |

### First login

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' podman-mcp-admin-password
```

That is the password the console was seeded with on its first boot. Open the console through your
tunnel (or `ssh -L 8080:127.0.0.1:8080 <host>` and <http://localhost:8080>), log in, and print the
connector URL with:

```bash
scripts/show-connector.sh --base https://podman-mcp.example.com
```

It reads the live token from `/data/config.json`, so it stays right after you rotate the token on
the Tokens page.

### Settings

Edit `~/.config/podman-mcp-admin/podman-mcp-admin.env` and re-run `scripts/install.sh`. The values
are rendered into the unit at install time, so a changed value restarts the container and an
unchanged one does not.

| Key | Default | Notes |
|-----|---------|-------|
| `MCP_ADMIN_BIND` | `127.0.0.1` | Publish address. A LAN IP only when the tunnel runs on another machine. `0.0.0.0` is refused. |
| `MCP_ADMIN_PORT` | `8080` | Host port. |
| `PODMAN_MCP_PROFILE` | `safe` | `readonly` (13 tools) / `safe` (18) / `full` (23) |
| `PODMAN_MCP_NAME_ALLOW` | *(empty)* | Regex of container/pod names the tools may touch (no spaces or quotes). |
| `PODMAN_API_VERSION` | the daemon's | Written by the first install. A version *newer* than the daemon 404s every call. |
| `PODMAN_MCP_MAX_CHARS` | `20000` | Per-call response ceiling; tools truncate by row and say how many they dropped. |
| `JWT_EXPIRY_HOURS` | `12` | Console session lifetime. |

**`/data/config.json` wins after the first boot.** The console self-seeds `config.json` on an empty
volume from `PODMAN_*`, `ADMIN_PASSWORD` and `MCP_AUTH_TOKEN` (see
`podman_mcp_admin/bootstrap.py`), and the values in that file override the container environment
from then on. A later change of `PODMAN_MCP_PROFILE` or `PODMAN_API_VERSION` in the env file
therefore does not reach a deployment whose `config.json` already holds it (a key that was empty at
first boot, such as an unset `PODMAN_MCP_NAME_ALLOW`, is not stored and still follows the env
file). Change stored values on the settings pages, or edit `config.json` in the volume and restart
the unit.

### Local development, no container

```bash
pip install -e ".[dev]"
python3 scripts/seed.py --config /tmp/pm/config.json \
    --podman-uri unix:///run/user/$(id -u)/podman/podman.sock
MCP_ADMIN_CONFIG=/tmp/pm/config.json JWT_SECRET=dev \
    uvicorn podman_mcp_admin.main:app --port 8080
```

Or the bare MCP server over stdio, no console at all:

```bash
PODMAN_MCP_PROFILE=readonly python3 -m woow_podman_mcp_server.server
```

---

## Authentication

1. **Network.** The console is published on `127.0.0.1` only, so only processes on the same host
   reach it: a cloudflared with host networking, or Nginx Proxy Manager. Widen `MCP_ADMIN_BIND`
   only when the tunnel runs on another machine, and then only to one LAN IP.
2. **Admin console.** One admin password, stored in `/data/config.json` (mode 0600). The session is
   an HS256 JWT signed with `JWT_SECRET`, sent in a cookie that is `HttpOnly`, `SameSite=Strict`,
   and `Secure` when the request arrived as HTTPS (`X-Forwarded-Proto: https`;
   `ADMIN_COOKIE_SECURE` overrides). It lasts `JWT_EXPIRY_HOURS`; logout or a password change
   revokes every issued token. Five failed logins within 300 s lock that client out for 30 s,
   doubling up to 900 s. The client is identified by the *first* `X-Forwarded-For` hop, which the
   client controls unless the proxy overwrites it, so the throttle only means something behind a
   same-host tunnel.
3. **MCP connector.** The URL `https://<host>/private_<token>/mcp/` is a **bearer credential**. It is
   compared in constant time and answers 403 on a mismatch. Rotating it from the Tokens page
   restarts the child. There is no OAuth: `/.well-known/*` and `/register` return a JSON 404 on
   purpose. The URL lands in the tunnel's access logs and in the claude.ai connector settings;
   treat it like a password.
4. **What a token holder can do.** Everything this user's podman socket can do, narrowed by
   `PODMAN_MCP_PROFILE` and `PODMAN_MCP_NAME_ALLOW`. `safe` (the default) includes `exec`, which
   reads any container's files and environment. Even `readonly` includes container inspect, which
   **returns every container's `Config.Env`, including podman secrets passed as `type=env`**
   (podman 4.9.3). The connector token is therefore also a read credential for the env secrets of
   every stack of that user, including this one's `JWT_SECRET`. Use `readonly` and a name
   allow-list for agents that only monitor.
5. **Recommended exposure.** A Cloudflare Access policy on the console hostname for everything
   **except** `/private_*`: the console then needs SSO plus the password, and the connector path
   stays reachable for claude.ai, which cannot perform an Access login. Or use a separate hostname
   for the connector if you do not want Access path rules.

The secrets reach the container as `type=env` podman secrets, so they never appear in the unit
files, `systemctl --user cat`, the container's create command or the journal. They do appear in
`podman inspect` of the running container (point 4). Moving them to file mounts needs `*_FILE`
support in `mcp_admin_core` first.

---

## Behind a Cloudflare tunnel

If cloudflared runs **on the same host** (for example with host networking), point its ingress at
`http://localhost:8080` (or your `MCP_ADMIN_PORT`). This is the default and needs nothing else.

If cloudflared runs **elsewhere**, for example an in-cluster cloudflared pod on a different
machine, `localhost` is that pod's own loopback and will 502. Publish on the Podman host's LAN
address and point the ingress there:

```bash
scripts/install.sh --bind 192.168.1.20        # this host's LAN IP; 0.0.0.0 is refused
```

```yaml
# cloudflared config.yaml ingress entry
- hostname: podman-mcp.example.io
  service: http://192.168.1.20:8080
```

Two consequences to accept first: the console and the connector path are then reachable by
**anything on that LAN**, behind a single admin password; and the host's LAN IP must be stable
(static lease), or the tunnel silently 502s. If that is more surface than you want, run a second
cloudflared on the Podman host pointing at localhost instead.

---

## Upgrade

```bash
git pull
scripts/upgrade.sh
```

`upgrade.sh` snapshots the installed units, exports `podman_mcp_data` with `scripts/backup.sh`,
runs `scripts/install.sh` (which builds the new `VERSION` tag and restarts what changed) and
`tests/smoke.sh`. If anything fails, it puts the previous units back, restarts them on the
previous image tag (install never deletes image tags) and exits 1. The version lives in
[`VERSION`](VERSION), the unit's `Image=` tag and `pyproject.toml`; CI fails when they disagree.

## Backup and restore

```bash
scripts/backup.sh                       # -> ~/backups/podman-mcp-admin/<timestamp>/
scripts/backup.sh --include-secrets     # also the three podman secrets, in secrets.env (0600)
scripts/backup.sh --stop               # stop the container around the export and start it again
scripts/restore.sh ~/backups/podman-mcp-admin/<timestamp>     # asks first; --yes to skip
```

The backup is a `podman volume export` of `podman_mcp_data`, whose `config.json` holds the admin
password and the live connector token: keep backups private. `restore.sh` stops the unit, replaces
the volume with the export, starts it and runs the smoke test.

## Uninstall

```bash
scripts/uninstall.sh                    # stop and remove the units; keep volume, secrets, image, env file
scripts/uninstall.sh --purge            # also delete the volume (final backup first), network and secrets
scripts/uninstall.sh --purge-images     # also remove the localhost/woow-podman-mcp-admin:* images
```

`--purge` is the only way these scripts delete data; it asks you to type the app name (`--yes`
skips that). Re-running `install.sh` after a plain uninstall adopts the same volume, so the
connector URL and the admin password are unchanged. `podman.socket` is never touched: other
stacks use it. The env file stays in `~/.config/podman-mcp-admin/`; delete it yourself.

## Migrating an existing deployment

For a host that runs the container from the old README (`podman run … podman-mcp-admin`, a
`podman generate systemd` unit or a hand-written `podman-podman-mcp-admin.service`):

1. Check the tunnel: the ingress for the console hostname must target `localhost:<port>` or
   `127.0.0.1:<port>` on this host. If it targets the host's LAN IP, plan `--bind <LAN IP>`.
2. Back up: `podman volume export podman_mcp_data -o ~/podman_mcp_data-pre-quadlet.tar` (0600),
   and keep a copy of the old unit file.
3. Stop and disable the old unit, and move it out of the way if its name is
   `podman-mcp-admin.service` (it would shadow the Quadlet unit):
   `systemctl --user disable --now podman-podman-mcp-admin.service`.
4. Keep the old container for rollback, stopped and renamed:
   `podman stop podman-mcp-admin && podman rename podman-mcp-admin podman-mcp-admin-legacy-$(date +%Y%m%d)`.
5. `scripts/install.sh`. It adopts `podman_mcp_data`, so the connector token and the admin password
   stay the same; only the console session (new `JWT_SECRET`) needs a fresh login.
6. `config.json` keeps the API version from its first boot. If `tests/smoke.sh` passes but tool
   calls answer 404, set the connection's API version to the daemon's
   (`podman version --format '{{.Server.APIVersion}}'`) on the settings page.
7. Rotate the admin password (it sat in the old container's environment and probably in shell
   history) and consider rotating the connector token.

Rollback: `scripts/uninstall.sh`, rename the legacy container back, re-enable the old unit.

## Docker and compose

Docker Compose is no longer part of this repo. The last commit with `docker-compose.yml` is tagged
[`compose-final`](https://github.com/WOOWTECH/Woow_podman__mcp_server/tree/compose-final); it is
not maintained. For Kubernetes use `Woow_k3s_mcp_server`. The image is built locally (`Pull=never`);
publishing it to GHCR is future work.

---

## Safety profiles

Tools are gated at **registration** time, not at list time. A tool outside the active profile does
not exist on the protocol: it cannot be called by name, cannot be reached by a client that cached
an older `tools/list`, and does not appear in the schema. A gate that only filters the *listing*
is bypassed by any client that already knows the tool name.

| Profile | Tools | Includes |
|---------|-------|----------|
| `readonly` | 13 | `ps`, `images`, `logs`, `inspect`, `stats`, `top`, `events`, `system_df`, `info`, `pods`, `networks`, `volumes`, `healthcheck` |
| `safe` *(default)* | 18 | + `start`, `stop`, `restart`, `exec`, `image_pull` |
| `full` | 23 | + `container_remove`, `image_remove`, `volume_remove`, `network_remove`, `system_prune` |

**This is the only meaningful boundary once the socket is mounted:** anything that can reach the
socket has that uid's full Podman; the profile is what narrows it.

---

## Security

**The Podman socket is the entire boundary.** libpod has no API key: anything that can reach the
socket can create a privileged container and bind-mount the host root, so it is root-equivalent
for that uid. The Quadlet unit mounts the *rootless* socket directory (`%t/podman`) and runs as
container root, which is the host user in the rootless user namespace. It also drops every
capability, sets `no-new-privileges` and a read-only root filesystem (`/data` is the only
writable volume).

**No OAuth.** The server answers every `/.well-known/*` probe and `/register` with a JSON `404`.
The SPA catch-all used to answer those probes with `200 text/html`, which a client reads as "yes,
I have an authorization server"; it then attempted Dynamic Client Registration, got HTML back, and
failed with *"Couldn't register with … 's sign-in service"* in a redirect loop. A clean 404 makes
discovery fail fast so the client falls back to anonymous access and just sends `initialize`.

**A remote host over `tcp://` has no authentication at all.** `PODMAN_URI=tcp://host:2376` is
supported by the client (with optional mTLS via `PODMAN_TLS_*`), but `podman system service` itself
does no TLS and no auth. Only use `tcp://` inside a trusted, isolated segment, and terminate mTLS in
front of it yourself. For an authenticated remote transport, prefer an SSH tunnel to the socket.

### Notes from the field

* **`podman stats` with an unknown name.** libpod answers `HTTP 200` with
  `{"Error": {}, "Stats": null}`, and `{}` is falsy, so the obvious `if payload.get("Error")` check
  never fires and the tool silently returns nothing. It also returns *no* stats when **any**
  requested name is unknown, so the error names the whole batch.
* **`podman top` with plain `ps` flags.** libpod returns *fewer* columns than titles for flag-style
  args like `aux`, so the rows cannot be tabulated. The tool detects the mismatch and prints the raw
  output with a hint to use descriptor form (`ps_args="-eo pid,user,comm"`).
* **Stream framing.** libpod is *always* 8-byte multiplexed, even with a TTY; only the Docker-compat
  `/v1.x` endpoints go raw. The `tty` flag is passed down explicitly rather than guessed from the
  payload, because output that happens to start with `\x01\x00\x00\x00` is otherwise eaten.

---

## Tests

```bash
pytest               # 33 tests, no network, no Podman required
tests/dryrun.sh      # renders the units and checks them with the podman 4.9.3 generator + systemd-analyze
tests/smoke.sh       # on an installed host: health, loopback-only, login, MCP initialize, wrong-token 403
```

CI: [`quadlet-ci.yml`](.github/workflows/quadlet-ci.yml) (vendored lib checksum, dry-run,
shellcheck) and [`tests.yml`](.github/workflows/tests.yml) (pytest, credential scan, image build).

## Layout

```
Dockerfile                  two-stage build (node 22.23.2 SPA stage, python 3.12.14 runtime), pinned
VERSION                     image tag; must match quadlet/podman-mcp-admin.container and pyproject.toml
quadlet/                    podman-mcp-admin.container, podman-mcp.volume, podman-mcp.network, render-vars
config/                     podman-mcp-admin.env.example
scripts/                    install, upgrade, uninstall, backup, restore, show-connector; lib/quadlet-lib.sh (vendored)
tests/                      pytest suite, dryrun.sh (+ dryrun.local.sh, fixtures/), smoke.sh
verification/               in-container client for manual checks against a live deployment
```

---

## Roadmap

Phase 1 (**this release**) is "the console comes up and the connector works". The MCP server is a
single self-contained `server.py`; the console supervises it, proxies it, streams its logs and
rotates its token.

| Phase | Scope |
|-------|-------|
| 1 ✅ | Console boots, self-seeds, auth, process supervision, encrypted proxy, 18/23 tools live |
| 2 | Profile data model: `registry.py`, `gating.py`, split `tools/`, GUI profile selector and per-tool toggles |
| 3 | Connection & health: real Podman probe, Test Connection with distinct errors per failure mode, full dashboard |
| 4 | Podman operations pages (containers, images, volumes, networks, pods) |

Until Phase 2/3 land, the **Connection** and **Tools** pages get a JSON `404` from the API fallback
and render empty. That is intentional and easier to debug than a stub that pretends to work.

---

## License

MIT; see [LICENSE](LICENSE).
