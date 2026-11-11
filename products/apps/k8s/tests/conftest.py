"""apps/k8s app-local 測試的共用 fixtures。

比照 core conftest：``JWT_SECRET`` 必須在任何 mcp_admin_core 模組被 import 之前固定
（auth/middleware.py 在 import 階段就讀取它），故在模組頂層 setdefault。

``make_client`` 工廠：以指定 connection / mcp_server 覆寫值寫入暫存 config.json，讓
get_config_store() singleton 以該檔重建，並用 core app factory 掛 config.router，回傳
已登入（帶 Bearer token）的 TestClient——因為 /api/config* 受 AuthMiddleware 保護。
"""
from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("JWT_EXPIRY_HOURS", "24")

import json
from pathlib import Path

import pytest


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """暫存 config.json 路徑（測試可直接讀回驗證存檔原值）。"""
    return tmp_path / "config.json"


@pytest.fixture
def make_client(config_path: Path, monkeypatch):
    """工廠 fixture：建 app + 已登入 TestClient。

    參數：
    - ``connection``：覆寫 config 的 connection section。
    - ``mcp_server``：淺合併進 config 的 mcp_server section（如設 port）。
    """
    import mcp_admin_core.config.store as store_mod

    def _make(connection: dict | None = None, mcp_server: dict | None = None):
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        if connection is not None:
            cfg["connection"] = connection
        if mcp_server is not None:
            cfg["mcp_server"] = {**cfg["mcp_server"], **mcp_server}
        config_path.write_text(json.dumps(cfg))

        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        store_mod.get_config_store()

        from fastapi.testclient import TestClient

        from mcp_admin_core.app import create_app
        from k8s_mcp_admin.routers import config

        app = create_app(title="K8s Test Admin", extra_routers=[config.router])
        client = TestClient(app)
        token = client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        return client

    return _make


def stored_connection(config_path: Path) -> dict:
    """讀回暫存 config.json 的 connection section（驗證存檔原值用）。"""
    return json.loads(config_path.read_text()).get("connection", {})


@pytest.fixture(autouse=True)
def sa_token_mounted(tmp_path_factory, monkeypatch):
    """預設模擬「Pod 有掛 ServiceAccount token」，讓 in-cluster 既有測試與執行環境無關
    （CI runner 可能在叢集內、也可能不在）。要測 PaaS「未掛載」情境的測試，自行把
    ``cluster_probe.IN_CLUSTER_TOKEN_PATH`` 指到不存在的路徑（#1352）。"""
    from k8s_mcp_admin import cluster_probe

    token = tmp_path_factory.mktemp("sa") / "token"
    token.write_text("fake-sa-token")
    monkeypatch.setattr(cluster_probe, "IN_CLUSTER_TOKEN_PATH", str(token))
    return token
