"""health base factory 契約測試：response 鍵逐字、overall_status 三態、async hooks。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(target_healthy: bool):
    from mcp_admin_core.routers.health import make_health_router

    async def probe_target(conn):
        return {"healthy": target_healthy, "url": conn.get("x_url", ""), "status_code": 200}

    async def fetch_info(conn):
        return {"version": "1.2.3", "db_name": "demo", "item_count": 7}

    app = FastAPI()
    app.include_router(make_health_router(
        app_type="fakeapp",
        probe_target=probe_target, fetch_info=fetch_info,
    ))
    return TestClient(app)


def test_health_keys_verbatim(store_env):
    c = _client(target_healthy=True)
    body = c.get("/api/health").json()
    assert set(body) == {
        "app_type", "overall_status", "mcp_server", "target_app",
        "proxy", "version", "db_name", "item_count",
    }
    assert body["app_type"] == "fakeapp"
    assert body["proxy"] == {"healthy": True, "process": "built-in reverse proxy"}
    assert body["version"] == "1.2.3" and body["db_name"] == "demo" and body["item_count"] == 7


def test_overall_status_error_when_all_down(store_env):
    c = _client(target_healthy=False)
    body = c.get("/api/health").json()
    # mcp 未啟動 + target 不健康 → error
    assert body["overall_status"] == "error"
    assert body["mcp_server"]["healthy"] is False
