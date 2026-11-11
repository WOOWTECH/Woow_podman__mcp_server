#!/usr/bin/env python3
"""Read-only HA source-policy proof; no HA service, identity or backend access."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


def check(root, commit):
    package = root / 'packages/mcp-admin-core'
    files = {}
    for name in ('products.py', 'surface.py', 'policy.py', 'config.py'):
        relative = 'packages/mcp-admin-core/mcp_admin_core/' + name
        expected = subprocess.check_output(['git', '-C', str(root), 'show', commit + ':' + relative])
        actual = (root / relative).read_bytes()
        assert actual == expected, f'HA source differs from pinned commit: {name}'
        files[name] = hashlib.sha256(actual).hexdigest()
    sys.path.insert(0, str(package))
    from mcp_admin_core.products import fresh_write_grants
    from mcp_admin_core.surface import full_tools
    from mcp_admin_core.config import FRESH_WRITES_ENABLED
    from mcp_admin_core.policy import authorize, ToolCallRefused
    tools, name = full_tools('emqx'), 'emqx_test_rule_sql'
    state = SimpleNamespace(disabled=[], enabled_write_tools=fresh_write_grants('emqx'),
                            writes_enabled=FRESH_WRITES_ENABLED, denied_operations={}, configured=True)
    def message():
        return {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': name, 'arguments': {'sql': 'SELECT * FROM "owned/#"'}}}
    allowed = authorize(message(), tools, state) == 'tools/call'
    state.enabled_write_tools.remove(name)
    try:
        authorize(message(), tools, state)
        revoked = False
    except ToolCallRefused:
        revoked = True
    assert allowed and revoked
    return {'source_commit': commit,
            'scope': 'released HA source policy only; no HAOS identity or real backend claim',
            'tool_count': len(tools), 'fresh_global_writes': FRESH_WRITES_ENABLED,
            'sql_write_classification': tools[name].write, 'sql_exact_grants': tools[name].grants(name),
            'fresh_sql_allowed': allowed, 'sql_revoked_while_global_writes_on_denied': revoked,
            'file_sha256': files}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ha_root', type=Path)
    parser.add_argument('--commit', default='28b8317ec12c47c81c946fe9aaec18dd1d5ad852')
    args = parser.parse_args()
    print(json.dumps(check(args.ha_root.resolve(), args.commit), indent=2))
