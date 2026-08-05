# Woow Podman MCP Server

A FastMCP server that exposes a **Podman host** through its libpod REST API as MCP tools, plus a
web admin console that supervises it, gates it, and publishes it on an authenticated URL that
Claude (or any MCP client) can connect to directly.

It runs as **one rootless Podman container that borrows the host's Podman socket** — the container
manages the host's containers by reaching the daemon over a bind-mounted socket, never by running a
Podman of its own. Three components live in that one container:

| # | Component | What it is |
|---|-----------|------------|
| 1 | `woow_podman_mcp_server` | The MCP server. 23 tools over the libpod API, gated by a safety profile. Binds to loopback only. |
| 2 | `podman_mcp_admin` | The admin console: React SPA + FastAPI, on `:8080`. Supervises component 1 as a child process. |
| 3 | `mcp_admin_core` | Product-agnostic plumbing shared with the other Woow MCP consoles: app factory, JWT auth, config store, process manager, reverse proxy. |

The connector URL is `https://<host>/private_<mcp_auth_token>/mcp/`. The path segment **is** the
credential — see [Security](#security).

---

## Deploy on rootless Podman (the supported path)

The daemon runs on the host; the console runs in a container; they meet at the socket. You never run
a second Podman inside the container — that would be nested-container hell. You hand the container a
window onto the host's one Podman.

```bash
# 1. The host's rootless Podman socket must be listening. --time=0 keeps the
#    daemon from sleeping after 5s idle (an MCP server holds a long connection).
systemctl --user enable --now podman.socket

# 2. Build the image (needs a machine that can reach your registry).
#    --format docker is load-bearing: podman builds OCI by default, and the
#    OCI image spec has no healthcheck field, so the Dockerfile's HEALTHCHECK
#    is silently dropped. The container then reports no health status at all
#    and `podman ps` shows an empty STATUS column instead of healthy/unhealthy.
git clone https://github.com/WOOWTECH/Woow_podman__mcp_server.git
cd Woow_podman__mcp_server
podman build --format docker -t podman-mcp-admin:0.1.0 .

# 3. Run it, bind-mounting the host socket in.
podman run -d --name podman-mcp-admin \
  --user "$(id -u):$(id -g)" \
  -p 8080:8080 \
  -v /run/user/$(id -u)/podman/podman.sock:/run/podman/podman.sock:z \
  -v podman_mcp_data:/data \
  -e JWT_SECRET="$(openssl rand -hex 32)" \
  -e ADMIN_PASSWORD="choose-a-strong-one" \
  -e PODMAN_URI=unix:///run/podman/podman.sock \
  -e PODMAN_MCP_PROFILE=safe \
  podman-mcp-admin:0.1.0
```

Then open <http://localhost:8080>, log in, and the connector URL is on the **Tokens** page.

### The four things that bite on rootless Podman

* **`:z` on the socket mount.** On SELinux hosts (RHEL/Fedora) a bare bind mount of the socket is
  visible but not connectable (`avc: denied`), which looks exactly like a permission-alignment bug.
  `:z` relabels it. Harmless on non-SELinux hosts.
* **`--user` must match the socket's owner.** The rootless socket is `srw------- <you>`; the
  container process must be that uid or `connect()` returns `EACCES`.
* **`--time=0` on the socket.** `podman.socket` is socket-activated and the daemon sleeps when idle;
  a long-lived MCP connection needs it to stay up. `systemctl --user enable --now podman.socket`
  handles this; a hand-run `podman system service` needs `--time=0` explicitly.
* **The container must survive logout and reboot.** Use systemd + linger (below), not a bare
  `podman run` in a shell.

### Make it a managed service

```bash
podman generate systemd --new --name podman-mcp-admin \
  > ~/.config/systemd/user/podman-mcp-admin.service
systemctl --user daemon-reload
systemctl --user enable --now podman.socket podman-mcp-admin.service
loginctl enable-linger "$USER"     # keep it running with nobody logged in
```

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

## Behind a Cloudflare tunnel

If cloudflared runs **on the same host**, point its ingress at `http://localhost:8080`.

If cloudflared runs **elsewhere** — a common case is an in-cluster cloudflared pod on a *different*
machine from the Podman host — `localhost` is that pod's own loopback and will 502. Point the
ingress at the Podman host's **LAN address** instead, and publish the container's port on that
address (the `-p 8080:8080` above already binds `0.0.0.0`):

```yaml
# cloudflared config.yaml ingress entry
- hostname: podman-mcp.example.io
  service: http://<podman-host-LAN-ip>:8080
```

Two consequences to accept before doing this:

* The console's `:8080` is now reachable by **anything on that LAN**, not just localhost — the
  login page and the connector path both. Combined with the public tunnel, the GUI is exposed on
  the LAN *and* the internet behind a single admin password. If that is more surface than you want,
  keep the GUI off the LAN and run a second cloudflared on the Podman host pointing at localhost.
* The host's LAN IP must be **stable** (static lease / MAC reservation). If it changes, the tunnel
  silently 502s until you update the ingress.

---

## Preserving an existing connector across a redeploy

The connector token lives in `/data/config.json`. To move an already-connected client onto a fresh
container **without re-pointing it**, pass the old token in and the bootstrap seeds it verbatim:

```bash
-e MCP_AUTH_TOKEN=<the existing token>
```

The URL `…/private_<that token>/mcp/` keeps working; the Claude app needs no change. Omit
`MCP_AUTH_TOKEN` and a fresh token is generated and logged once — then every client must be
re-pointed.

---

## Safety profiles

Tools are gated at **registration** time, not at list time. A tool outside the active profile does
not exist on the protocol — it cannot be called by name, cannot be reached by a client that cached
an older `tools/list`, and does not appear in the schema. This is deliberate: a gate that only
filters the *listing* is bypassed by any client that already knows the tool name.

| Profile | Tools | Includes |
|---------|-------|----------|
| `readonly` | 13 | `ps`, `images`, `logs`, `inspect`, `stats`, `top`, `events`, `system_df`, `info`, `pods`, `networks`, `volumes`, `healthcheck` |
| `safe` *(default)* | 18 | + `start`, `stop`, `restart`, `exec`, `image_pull` |
| `full` | 23 | + `container_remove`, `image_remove`, `volume_remove`, `network_remove`, `system_prune` |

Set with `PODMAN_MCP_PROFILE`. **This is the only meaningful boundary once the socket is mounted:**
anything that can reach the socket has that uid's full Podman — the profile is what narrows it, and
it narrows at registration so a disabled tool is not merely hidden. Keep it at `safe` unless you
specifically need the destructive tools.

---

## Security

**The Podman socket is the entire boundary.** libpod has no API key — anything that can reach the
socket can create a privileged container and bind-mount the host root, i.e. it is root-equivalent
for that uid. Two rules:

1. Mount the **rootless** socket (`/run/user/<uid>/podman/podman.sock`), never the root service's,
   and run the container as that same uid.
2. Keep the profile at `safe`. It is the one control still available after the socket is mounted.

**No OAuth.** The server answers every `/.well-known/*` probe and `/register` with a JSON `404`.
This is not an omission — it is the fix. The SPA catch-all used to answer those probes with
`200 text/html`, which a client reads as "yes, I have an authorization server"; it then attempted
Dynamic Client Registration, got HTML back, and failed with *"Couldn't register with … 's sign-in
service"* in a redirect loop. A clean 404 makes discovery fail fast so the client falls back to
anonymous access and just sends `initialize`.

**The path token is the credential.** It is compared with `secrets.compare_digest`, never echoed
back unmasked, and rotating it from the Tokens page restarts the child. Put the console behind a
tunnel with TLS; do not expose `:8080` directly to the internet.

**A remote host over `tcp://` has no authentication at all.** `PODMAN_URI=tcp://host:2376` is
supported by the client (with optional mTLS via `PODMAN_TLS_*`), but `podman system service` itself
does no TLS and no auth — a bare TCP socket is an unauthenticated root API on the network. Only use
`tcp://` inside a trusted, network-isolated segment, and terminate mTLS in front of it yourself. For
a genuinely authenticated remote transport, prefer an SSH tunnel to the socket.

---

## Configuration

Everything lives in `/data/config.json` (`MCP_ADMIN_CONFIG`), written atomically and `chmod 600`.
A bare container **self-seeds** on first boot (see `podman_mcp_admin/bootstrap.py`): the child
command line, the `connection` block from `PODMAN_*` env, and the connector token from
`MCP_AUTH_TOKEN`. The `connection` section is upper-cased into the child's environment, so
`podman_uri` arrives as `PODMAN_URI`.

See [`.env.example`](.env.example) for the full list. The ones that matter:

| Variable | Default | Notes |
|----------|---------|-------|
| `PODMAN_URI` | `unix:///run/podman/podman.sock` | `tcp://host:2376` for a remote host (unauthenticated — see Security) |
| `PODMAN_API_VERSION` | `v5.0.0` | A version *newer* than the daemon 404s every call |
| `PODMAN_MCP_PROFILE` | `safe` | `readonly` / `safe` / `full` |
| `PODMAN_MCP_MAX_CHARS` | `20000` | Per-call response ceiling; tools truncate by row and say how many they dropped |
| `MCP_AUTH_TOKEN` | *(generated, logged once)* | Set it to preserve a connector across a redeploy |
| `JWT_SECRET` | *(random per process)* | Set it, or every restart invalidates sessions |
| `ADMIN_PASSWORD` | *(generated, logged once)* | First boot only |

### Notes from the field

* **`podman stats` with an unknown name.** libpod answers `HTTP 200` with
  `{"Error": {}, "Stats": null}` — and `{}` is falsy, so the obvious `if payload.get("Error")` check
  never fires and the tool silently returns nothing. It also returns *no* stats when **any**
  requested name is unknown, not just the bad one, so the error names the whole batch rather than
  accusing a container that is running fine.
* **`podman top` with plain `ps` flags.** libpod returns *fewer* columns than titles for flag-style
  args like `aux`, so the rows cannot be tabulated. The tool detects the mismatch and prints the raw
  output with a hint to use descriptor form (`ps_args="-eo pid,user,comm"`) instead of producing a
  column-shifted table.
* **Stream framing.** libpod is *always* 8-byte multiplexed, even with a TTY; only the Docker-compat
  `/v1.x` endpoints go raw. The `tty` flag is passed down explicitly rather than guessed from the
  payload, because output that happens to start with `\x01\x00\x00\x00` is otherwise eaten.

---

## Tests

```bash
pytest        # 22 tests, no network, no Podman required
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

MIT — see [LICENSE](LICENSE).
