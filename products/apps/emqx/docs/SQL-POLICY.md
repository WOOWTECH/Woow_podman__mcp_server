# SQL permission decision, 2026-10-30

Released HA source `28b8317ec12c47c81c946fe9aaec18dd1d5ad852` classifies
`emqx_test_rule_sql` as `unverified`, which `full_tools()` converts into a write
tool. Its exact required grant is the tool name. `FRESH_WRITES_ENABLED=True` and
`fresh_write_grants('emqx')` include that grant for new installations. Removing the
exact grant denies SQL even while the global write flag stays on. This is an
explicit, individually revocable permission **with an enabled default**.

Evidence: [executable source-policy receipt](ha-sql-default-proof.json), including
the SHA-256 of HA `surface.py`, `products.py`, `policy.py` and `config.py`. Those
files were checked unchanged from the released source. The check calls actual HA
`authorize()` once with fresh grants and once after removing SQL's grant. It does
not claim a real backend call or HAOS ingress identity.

Reproduce against a checkout whose policy files match the pinned commit:

```bash
python apps/emqx/scripts/check_ha_sql_policy.py /path/to/Woow_ha_mcp_addons
```

PaaS mapping uses its established deny-list model: all39 tools enabled on a new
installation, SQL marked write/execute rather than read, whole-tool or execute
revocation checked before backend I/O at both proxy and child. An independent
permission is not a new user request for default-off. The default-off seed in the
first checkpoint `92387f8` was the implementer's inference; it is removed by this
correction. This lane has not deployed either source checkpoint.

Tests cover both policies: fresh all-on lists39 and permits an authenticated SQL
request; revoked SQL is hidden/refused with no backend request, stays revoked over
restart, and can be re-enabled through the standard authenticated revisioned route.
N13/N15 tests use actual merged PR95 core, without a compatibility skip or local
shared-core implementation.
