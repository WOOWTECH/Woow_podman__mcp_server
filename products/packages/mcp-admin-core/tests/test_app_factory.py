"""create_app 工廠回歸測試 —— 基本組裝、extra_routers 注入並受 auth 保護。"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict


def test_create_app_exposes_healthz(make_app):
    c = make_app()
    # mcp：給平台的粗略狀態（沒有政策、子行程沒在跑；三種值見 test_setup_status.py）
    assert c.get("/healthz").json() == {"status": "ok", "mcp": "not_running"}


def test_extra_router_is_mounted_and_auth_protected(tmp_config_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    tmp_config_path.write_text(json.dumps(dict(store_mod._DEFAULT_CONFIG)))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    store_mod.get_config_store()

    from mcp_admin_core import create_app

    extra = APIRouter()

    @extra.get("/api/ping")
    async def ping():
        return {"pong": True}

    app = create_app(title="Factory Test", extra_routers=[extra])
    assert app.title == "Factory Test"

    c = TestClient(app)
    # extra router 上的 /api/* 也受 middleware 保護
    assert c.get("/api/ping").status_code == 401
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    r = c.get("/api/ping", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"pong": True}


def test_tokens_router_strong_mounted(make_app):
    c = make_app()
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    r = c.get("/api/tokens", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_factory_routers_mount_at_expected_prefixes():
    from enum import Enum
    from types import SimpleNamespace
    from fastapi import APIRouter
    from fastapi.testclient import TestClient
    import mcp_admin_core.config.store as store_mod
    from mcp_admin_core import create_app
    from mcp_admin_core.routers.logs import create_logs_router
    from mcp_admin_core.routers.tools import create_tools_router
    from mcp_admin_core.routers.health import make_health_router

    class _Cat(str, Enum):
        A = "A"

    reg = SimpleNamespace(
        TOOL_REGISTRY=[SimpleNamespace(name="t", category=_Cat.A, description="d",
                                       operations=[], dangerous=False)],
        ToolCategory=_Cat, get_all_tool_names=lambda: ["t"],
        get_tool_by_name=lambda n: None,
    )

    async def probe(conn): return {"healthy": True, "url": ""}
    async def fetch(conn): return {"version": "1", "db_name": "x", "item_count": 0}

    app = create_app(title="Wiring", extra_routers=[
        create_logs_router("wiring_test"),
        create_tools_router(registry=reg, mcp_label="x-mcp"),
        make_health_router(app_type="x", probe_target=probe, fetch_info=fetch),
    ])

    # 此環境（starlette 1.3.1 / fastapi 0.138.1）的 include_router 會把 router
    # 包成 _IncludedRouter（具 original_router），不會把子路由攤平進 app.routes，
    # 故遞迴蒐集各層 .path（含 original_router.routes）以驗證掛載前綴。
    def _collect_paths(routes) -> set[str]:
        paths: set[str] = set()
        for r in routes:
            p = getattr(r, "path", None)
            if p:
                paths.add(p)
            orig = getattr(r, "original_router", None)
            if orig is not None and hasattr(orig, "routes"):
                paths |= _collect_paths(orig.routes)
            elif not p and hasattr(r, "routes"):
                paths |= _collect_paths(r.routes)
        return paths

    paths = _collect_paths(app.routes)
    assert "/api/logs/stream" in paths
    assert "/api/tools" in paths
    assert "/api/health" in paths
    assert "/api/tokens" in paths   # 強掛


class _EchoProbeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    password: str
    port: int = 1


def test_validation_errors_name_the_field_but_never_echo_submitted_values(make_app):
    """FastAPI 預設的 422 每一筆都帶 input（送出的值；缺必填欄位時是整個 body），pydantic 的
    hide_input_in_errors 管不到。create_app 的處理照舊回 type／loc／msg，只拿掉 input。"""
    from fastapi import APIRouter

    probe = APIRouter()

    @probe.put("/api/echo-probe")
    async def echo_probe(body: _EchoProbeBody):
        return {"ok": True}

    c = make_app()
    c.app.include_router(probe)
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    r = c.put("/api/echo-probe", json={"passwrod": "canary-secret-1", "port": "canary-secret-2"},
              headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422
    assert "canary-secret" not in r.text
    assert sorted((e["type"], e["loc"]) for e in r.json()["detail"]) == [
        ("extra_forbidden", ["body", "passwrod"]), ("int_parsing", ["body", "port"]), ("missing", ["body", "password"])]
    assert all("input" not in e and e["msg"] for e in r.json()["detail"])
