# Woow Podman MCP Server

A FastMCP server that exposes a **Podman host** through its libpod REST API as MCP tools, plus a
web admin console that supervises it, gates it, and publishes it on an authenticated URL that
Claude (or any MCP client) can connect to directly.

Three components, one container:

| # | Component | What it is |
|---|-----------|------------|
| 1 | `woow_podman_mcp_server` | The MCP server. 23 tools over the libpod API, gated by a safety profile. Binds to loopback only. |
| 2 | `podman_mcp_admin` | The admin console: React SPA + FastAPI, on `:8080`. Supervises component 1 as a child process. |
| 3 | `mcp_admin_core` | Product-agnostic plumbing shared with the other Woow MCP consoles: app factory, JWT auth, config store, process manager, reverse proxy. |

The connector URL is `https://<host>/private_<mcp_auth_token>/mcp/`. The path segment **is** the
credential — see [Security](#security).

---

## Quick start

```bash
git clone https://github.com/WOOWTECH/Woow_podman__mcp_server.git
cd Woow_podman__mcp_server

# Podman's rootless socket must be running.
systemctl --user enable --now podman.socket

PODMAN_SOCK=/run/user/$(id -u)/podman/podman.sock \
PODMAN_UID=$(id -u) PODMAN_GID=$(id -g) \
JWT_SECRET=$(openssl rand -hex 32) \
docker compose up --build
```

Then:

1. Open <http://localhost:8080>. The admin password is printed once in the first-boot logs
   (`ADMIN_PASSWORD not set — generated one for first boot: …`), or set `ADMIN_PASSWORD` yourself.
2. **Tokens** → *Rotate* to mint the MCP auth token. It is shown once.
3. Point your client at `http://localhost:8080/private_<that token>/mcp/`.

To skip the GUI entirely, seed the config file directly:

```bash
python3 scripts/seed.py --config ./data/config.json --profile safe
```

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

Set with `PODMAN_MCP_PROFILE`. In Kubernetes the profile lives in the Secret, so changing it
requires a rollout — the GUI deliberately cannot widen it.

---

## Security

**The Podman socket is the entire boundary.** Anything that can reach it can start a privileged
container and therefore own the host account. Three rules:

1. Mount the **rootless** socket (`/run/user/<uid>/podman/podman.sock`), never the root service's.
2. Run the container as that same uid/gid. A bind mount the process cannot connect to fails with
   `EACCES`, which reads like a bug rather than a permission problem.
3. Leave the profile at `safe` unless you specifically need the destructive tools.

**No OAuth.** The server answers every `/.well-known/*` probe and `/register` with a JSON `404`.
This is not an omission — it is the fix. The SPA catch-all used to answer those probes with
`200 text/html`, which a client reads as "yes, I have an authorization server"; it then attempted
Dynamic Client Registration, got HTML back, and failed with *"Couldn't register with … 's sign-in
service"* in a redirect loop. A clean 404 makes discovery fail fast so the client falls back to
anonymous access and just sends `initialize`.

**The path token is the credential.** It is compared with `secrets.compare_digest`, never echoed
back unmasked, and rotating it from the Tokens page restarts the child. Put the console behind a
tunnel with TLS; do not expose `:8080` directly.

---

## Configuration

Everything lives in `/data/config.json` (`MCP_ADMIN_CONFIG`), written atomically and `chmod 600`.
The `connection` section is upper-cased into the child's environment, so `podman_uri` arrives as
`PODMAN_URI`.

See [`.env.example`](.env.example) for the full list. The ones that matter:

| Variable | Default | Notes |
|----------|---------|-------|
| `PODMAN_URI` | `unix:///run/podman/podman.sock` | `tcp://host:2376` for a remote host |
| `PODMAN_API_VERSION` | `v5.0.0` | A version *newer* than the daemon 404s every call |
| `PODMAN_MCP_PROFILE` | `safe` | `readonly` / `safe` / `full` |
| `PODMAN_MCP_MAX_CHARS` | `20000` | Per-call response ceiling; tools truncate by row and say how many they dropped |
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

## Development

```bash
pip install -e ".[dev]"
pytest                      # 22 tests, no network, no Podman required
cd frontend && npm install && npm run dev
```

Run the console against a local Podman without Docker:

```bash
python3 scripts/seed.py --config /tmp/pm/config.json \
    --podman-uri unix:///run/user/$(id -u)/podman/podman.sock
MCP_ADMIN_CONFIG=/tmp/pm/config.json JWT_SECRET=dev \
    uvicorn podman_mcp_admin.main:app --port 8080
```

Or run the MCP server on its own, with no console at all:

```bash
PODMAN_MCP_PROFILE=readonly python3 -m woow_podman_mcp_server.server   # stdio
```

---

## Roadmap

Phase 1 (**this release**) is "the console comes up and the connector works". The MCP server is a
single self-contained `server.py`; the console supervises it, proxies it, streams its logs and
rotates its token.

| Phase | Scope |
|-------|-------|
| 1 ✅ | Console boots, auth, process supervision, encrypted proxy, 18/23 tools live |
| 2 | Profile data model: `registry.py`, `gating.py`, split `tools/`, GUI profile selector and per-tool toggles |
| 3 | Connection & health: real Podman probe, Test Connection with distinct errors per failure mode, full dashboard |
| 4 | Kubernetes manifests, Cloudflare tunnel, socket-security docs |
| 5 | Podman operations pages (containers, images, volumes, networks, pods) |

Until Phase 2/3 land, the **Connection** and **Tools** pages get a JSON `404` from the API fallback
and render empty. That is intentional and easier to debug than a stub that pretends to work.

---

## License

MIT — see [LICENSE](LICENSE).
