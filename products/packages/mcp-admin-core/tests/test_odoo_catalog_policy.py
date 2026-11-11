"""The pinned Odoo MCP tools must be visible to admins and enforceable by the proxy."""

from management_client import revision_headers
import hashlib
import inspect
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_admin_core.routers.tools import create_tools_router
from mcp_admin_core.tool_filter import disabled_calls, filter_json_body
from odoo_mcp_admin import tool_registry


PUBLISHED_PACKAGE_SHA = {
    "tools_diagnostics.py": "1355ad597fbe67cf4222c5c2295e229265c6654ebbfd83cebb62a7f291346bd5",
    "tools_data_quality.py": "729066d00e28f7ef27db35577b6604f239f96084b75761c63a6181c87072d761",
}
EXTRA = {"analyze_upgrade_log", "data_quality_report"}


def test_pinned_upstream_extra_tools_are_managed_and_disableable(store_env):
    from odoo_mcp.server import mcp
    from odoo_mcp import tools_data_quality, tools_diagnostics

    for module in (tools_diagnostics, tools_data_quality):
        path = Path(module.__file__)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == PUBLISHED_PACKAGE_SHA[path.name]
    assert callable(tools_diagnostics.analyze_upgrade_log)
    assert callable(tools_data_quality.data_quality_report)
    assert inspect.signature(tools_data_quality.data_quality_report).parameters["model"]

    names = tool_registry.get_all_tool_names()
    assert len(names) == len(set(names)) == 41
    assert set(names) == set(mcp._tool_manager._tools)
    app = FastAPI()
    app.include_router(create_tools_router(registry=tool_registry, mcp_label="odoo-mcp"))
    client = TestClient(app)
    listed = client.get("/api/tools")
    assert listed.status_code == 200
    assert listed.json()["total"] == 41
    assert EXTRA <= {t["name"] for t in listed.json()["tools"]}
    assert all(not tool_registry.get_tool_by_name(name).dangerous for name in EXTRA)

    saved = client.put("/api/tools", json={"disabled_tools": sorted(EXTRA)}, headers=revision_headers(client, None, read_path='/api/tools'))
    assert saved.status_code == 200
    assert set(saved.json()["disabled_tools"]) == EXTRA
    disabled = client.get("/api/tools").json()
    assert {t["name"] for t in disabled["tools"] if not t["enabled"]} == EXTRA
    assert disabled["total"] == 41 and disabled["disabled"] == 2

    listed_upstream = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": n} for n in names]}}
    filtered = json.loads(filter_json_body(json.dumps(listed_upstream).encode(), frozenset(EXTRA)))
    assert {t["name"] for t in filtered["result"]["tools"]} == set(names) - EXTRA
    requests = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": {}}}
                for i, n in enumerate(sorted(EXTRA), 1)]
    assert disabled_calls(requests, frozenset(EXTRA)) == requests
