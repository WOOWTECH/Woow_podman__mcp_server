# Verification harness

`mcpc.py` is a tiny in-pod client for driving the **real** admin API and the
**real** MCP endpoint of a running `podman-mcp-admin`. It is not a unit test and
it does not run in CI — the hermetic tests live in `tests/`. This one needs the
pod's network namespace and filesystem to mean anything at all, because the
whole point is to exercise the deployed thing rather than a stand-in.

It reads its credentials at runtime from `/data/config.json`, taking the admin
console password and the MCP auth token from there. No secret is stored in this
directory and none should ever be added: a literal token here would be a
plaintext production credential living in the repository, and it would also make
every assertion that used it silently vacuous the moment the token was rotated.

`mcpc.py` loads that config, logs in to obtain a JWT, wraps the admin REST API,
and implements a small stateful streamable-HTTP MCP session that can list and
call tools. Import it; do not run it on its own.

```python
import mcpc

status, body = mcpc.api('/api/health')
assert body['app_type'] == 'podman'

with mcpc.Session() as s:
    names = s.list_tools()
    print(len(names), 'tools')          # 13 / 18 / 23 by profile
    print(mcpc.text_of(s.call('podman_info')))
```

## Phase 1 scope

The litellm original shipped five `t_*.py` suites alongside this client. They
are deliberately absent here rather than ported half-working: each one asserts
against routers this build does not register yet (`/api/config`, `/api/tools`)
or against a tool surface Phase 2 will restructure. A suite that fails for the
wrong reason teaches an operator to ignore the harness.

They land with the features they check:

| Suite | Lands with | Checks |
|-------|------------|--------|
| `t_pages.py` | Phase 3 | every console page's backing API is reachable, authenticated and Podman-shaped |
| `t_settings.py` | Phase 2 | every switch on the settings pages really changes the live MCP surface |
| `t_logs.py` | — | the log page end to end, including the SSE stream |
| `t_tools_ro.py` | Phase 2 | profile gating: a gated tool explains itself rather than answering "Unknown tool" |
| `t_tools_all.py` | Phase 5 | all 23 `podman_*` tools over the live endpoint, creating only `mcptest-`-prefixed resources and tearing them down afterwards |

When they arrive: run each with `python3 t_xxx.py` from a directory that also
contains `mcpc.py`, since each suite adds its own directory to `sys.path` to
import it. Any suite that mutates configuration must restore what it changed in
a `finally` block — an interrupted run must not leave the connector crippled.
