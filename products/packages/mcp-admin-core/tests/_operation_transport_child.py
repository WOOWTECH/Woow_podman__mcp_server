"""Hermes 真實 MCP 子行程；Dashboard 以記錄檔替身取代，絕不連線外部。"""
import json
import sys
from pathlib import Path

import httpx
import uvicorn

from hermes_mcp_server import server as hermes
from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

port, audit = int(sys.argv[1]), Path(sys.argv[2])
sessions = {"fixture": {"runtime_revision": 7, "runtime_generation": 3}}
receipts = {}


def response(request):
    if request.method == "GET" and request.url.path == "/api/sessions":
        # Session list returns object rows; bare IDs are malformed Dashboard data.
        return httpx.Response(200, json={"sessions": [{"id": sid} for sid in sessions]})
    if request.method == "GET" and request.url.path == "/api/sessions/fixture/mutation-snapshot":
        row = sessions.get("fixture")
        return httpx.Response(200, json={
            "session_id": "fixture", "exists": row is not None,
            "runtime_revision": row["runtime_revision"] if row else 0,
            "runtime_generation": row["runtime_generation"] if row else None})
    if request.method == "DELETE" and request.url.path == "/api/sessions/fixture":
        identity = dict(request.url.params)
        if set(identity) != {"request_id", "expected_revision", "expected_generation"}:
            return httpx.Response(409, json={"detail": "client_upgrade_required"})
        key = identity["request_id"]
        if key in receipts:
            previous, result = receipts[key]
            return httpx.Response(200 if identity == previous else 409,
                json=result if identity == previous else {"detail": "admission_conflict"})
        row = sessions.get("fixture")
        if row is None:
            return httpx.Response(200, json={"ok": True, "already_absent": True})
        if (identity["expected_revision"] != str(row["runtime_revision"])
                or identity["expected_generation"] != str(row["runtime_generation"])):
            return httpx.Response(409, json={"detail": "revision_conflict"})
        del sessions["fixture"]
        result = {"ok": True, "deleted_ids": ["fixture"]}
        receipts[key] = (identity, result)
        return httpx.Response(200, json=result)
    return httpx.Response(404, json={"detail": "Unknown fixture route"})


def backend(request):
    existed = "fixture" in sessions
    result = response(request)
    with audit.open("a") as stream:
        stream.write(json.dumps({
            "method": request.method, "path": request.url.path,
            "params": dict(request.url.params), "status": result.status_code,
            "session_exists": "fixture" in sessions,
            "committed": existed and "fixture" not in sessions}) + "\n")
    return result


async def dashboard(_connection):
    return httpx.AsyncClient(base_url="http://fixture.invalid", transport=httpx.MockTransport(backend))


hermes._get_connection = lambda: {"dashboard_url": "http://fixture.invalid", "gateway_url": "http://fixture.invalid", "gateway_api_key": "fixture"}
hermes._get_dashboard_client = dashboard
uvicorn.run(DualTransportApp(hermes.server), host="127.0.0.1", port=port, log_level="warning")
