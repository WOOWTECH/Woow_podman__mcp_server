# EMQX MCP Admin — source candidate

PaaS adapter for the released HA 39-tool EMQX surface. **This source candidate is
not a published PaaS product.** Root workspace/lock and CI wiring are included;
independent review, CI approval, merge, image publication and exact-image
real-broker acceptance remain release gates owned by main.

The app uses the shared admin factory, authentication, token proxy, configuration
store, process manager, Tools/operations, health and logs pages. Configure the
broker root URL, API key and API secret through Connection. Both credential fields
are write-only and masked in management reads; blank or the displayed mask retains
the existing value. Connection test reads the saved values and authenticates to
`/api/v5/nodes`, requiring a broker node/version response rather than any HTTP 200.

The seven groups contain 39 tools. The original 13 risk-marked tools keep their
classification. `emqx_test_rule_sql` is additionally classified as write/execute.
All 39 tools start enabled, matching released HA's all-on owner default, which
includes SQL's exact write grant. An administrator can revoke its whole-tool or
execute permission independently; subsequent choices survive restart. There is no
app-specific default-off SQL seed. See the [policy decision](docs/SQL-POLICY.md).
The two mixed tools accept exact `read`, `create`, or `delete` selectors. Policy is
checked at the shared proxy and again in the child; changes take effect in the same
MCP session without an app-side restart route or middleware.

The installed `emqx-mcp-server --port 8000` entrypoint binds only `127.0.0.1`, serves
Streamable HTTP at `/mcp`, and reads connection credentials from `EMQX_MCP_*` env
injected by the core. `MCP_ADMIN_CONFIG` must identify the app-owned 0600 regular
single-link saved configuration. Missing, corrupt or unsafe policy denies calls;
the child never creates or repairs the config. The outer authenticated proxy is the
public MCP endpoint. Production must use the shared deployment's admin/JWT settings.

The app HTTP client validates URLs and all DNS answers under deployment-owned
`WEBHOOK_SECURITY_MODE`, connects to the validated numeric address, preserves TLS
hostname verification, ignores environment proxies/CA overrides, refuses redirects,
and bounds concurrency, timeouts and response bytes. It uses reviewed HTTPX 0.28.1 /
httpcore 1.0.9 internals with source-hash checks. A dependency change requires review;
it fails closed instead of silently changing that transport boundary. Private
networks require the deployment's authorized mode; this is not an editable app
connection setting. Escaped traversal, double-encoded paths, non-identity compressed
responses and responses over 4 MiB are refused. Broker HTTP failures are generic
errors without reflected bodies or credentials. A failed delete is never retried as
a publish. The retained read listing fallback is used only for a direct 404.

Install and test from the repository root (CI uses uv 0.6.4 and Python 3.12):

```bash
uv sync --frozen --package woow-emqx-mcp-server --extra dev --python 3.12
uv run --no-sync --directory apps/emqx pytest -q
uv run --no-sync --directory apps/emqx python scripts/ci_smoke_child.py
npm ci --no-audit --no-fund
npm run build -w apps/emqx/frontend
```

Tests use synthetic credentials and owned local HTTP servers. They cover all 39
positive and denied calls, schemas, strict selectors, actual proxy/child processes,
error/no-retry behavior, DNS pinning, TLS rejection, redaction, SQL grants, persistence
and cleanup. They **do not** establish real EMQX edition/version support, MQTT
effects, PVC persistence or HAOS ingress identity. The earlier HA real-broker
acceptance is separate evidence and cannot establish acceptance for this new image.

N13/N15 tests now run unconditionally against merged core
`3c93d0e727fdc948f1ea17cb5fa7d1ad1887a883` (PR95), the branch's integration base.
The earlier checkpoint `92387f8` tested baseline `7f0eb78` and separately imported
Aristotle's candidate core; it is preserved for history and is superseded by this
all-on policy correction. See [integration handoff](docs/INTEGRATION.md) and
[provenance](VENDORED.md).
