"""匿名 OpenAPI 與 SPA 萬用 fallback 回歸測試（商用驗收 anonymous-openapi-catchall）。

- /docs、/redoc、/openapi.json 預設不開（匿名可讀會洩漏整份管理 API 地圖）；
  只有設 ``MCP_ADMIN_API_DOCS=1`` 才開，方便本機開發。
- SPA fallback 只對前端已知路由回 index.html；/.env、/.git/config、/metrics、
  /api/<不存在>、/assets 以外的缺檔一律 404，不再「什麼路徑都 200」。
"""

from __future__ import annotations

import json

import pytest

INDEX = "<html><body>admin-spa</body></html>"


@pytest.fixture
def spa_client(tmp_path, tmp_config_path, monkeypatch):
    """帶 static/index.html 的 app；可用 env 覆寫 docs 開關。"""
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX)
    (static / "assets" / "app.js").write_text("console.log(1)")

    tmp_config_path.write_text(json.dumps(dict(store_mod._DEFAULT_CONFIG)))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    store_mod.get_config_store()

    def _make(**env: str) -> TestClient:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from mcp_admin_core import create_app

        return TestClient(create_app(title="SPA Test", static_dir=static))

    return _make


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_api_docs_disabled_by_default(spa_client, monkeypatch, path):
    monkeypatch.delenv("MCP_ADMIN_API_DOCS", raising=False)
    c = spa_client()
    r = c.get(path)
    assert r.status_code == 404, f"{path} 應為 404，實得 {r.status_code}"
    assert "admin-spa" not in r.text
    assert "openapi" not in r.text.lower()


def test_api_docs_can_be_enabled_by_env(spa_client):
    c = spa_client(MCP_ADMIN_API_DOCS="1")
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/docs").status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/", "/login", "/config", "/tools", "/tokens", "/logs", "/permissions", "/settings", "/tools/"],
)
def test_spa_known_routes_serve_index(spa_client, path):
    c = spa_client()
    r = c.get(path)
    assert r.status_code == 200
    assert r.text == INDEX


@pytest.mark.parametrize(
    "path",
    [
        "/.env",
        "/.git/config",
        "/.well-known/security.txt",
        "/tools/.env",
        "/metrics",
        "/api",
        "/admin",
        "/health",
        "/robots.txt",
        "/debug/vars",
        "/actuator/health",
        "/server-status",
        "/wp-admin/install.php",
        "/backup.zip",
        "/favicon.ico",
        "/api/auth/nope",
        "/assets/missing.js",
        "/config/../.env",
        "/tools/extra/deep",
    ],
)
def test_spa_unknown_paths_return_404(spa_client, path):
    c = spa_client()
    r = c.get(path)
    assert r.status_code == 404, f"{path} 應為 404，實得 {r.status_code}"
    assert "admin-spa" not in r.text


def test_unknown_api_route_is_401_then_404(spa_client):
    """/api/* 未登入先被 auth 擋 401；登入後不存在的 API 是 404，不是 index.html。"""
    c = spa_client()
    assert c.get("/api/not-a-route").status_code == 401
    token = c.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    r = c.get("/api/not-a-route", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404
    assert "admin-spa" not in r.text


def test_spa_routes_override(spa_client, tmp_path):
    from fastapi.testclient import TestClient
    from mcp_admin_core import create_app

    c = TestClient(create_app(title="x", static_dir=tmp_path / "static", spa_routes={"", "custom"}))
    assert c.get("/custom").status_code == 200
    assert c.get("/tools").status_code == 404


def test_existing_routes_still_work(spa_client):
    c = spa_client()
    assert c.get("/healthz").json() == {"status": "ok", "mcp": "not_running"}
    assert c.get("/assets/app.js").status_code == 200
    # /api/* 未登入仍是 401（auth middleware 在 404 之前）
    assert c.get("/api/settings").status_code == 401
