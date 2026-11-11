"""Connection 頁實際送出的欄位（產品 ``PUT /api/config/connection`` 的「GUI payload」）與拒收拼錯欄位的斷言。

共用的 Connection 頁（``packages/mcp-admin-ui/src/pages/ConnectionConfig.jsx``）存檔時送出的 JSON 就是各產品
``frontend/src/appConfig.jsx`` 裡 ``connection.sections[].fields[].name`` 的全部欄位（值是表單內容），不多不少，也不送
``restart``。產品的連線模型是 ``extra='forbid'``：頁面多送一個模型沒宣告的欄位，整頁就存不進去（422），所以測試的
GUI payload 以這裡從 appConfig.jsx 讀到的欄位名為準，不在測試裡另抄一份。
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

APPS = Path(__file__).resolve().parents[3] / "apps"


def gui_connection_fields(app: str) -> set[str]:
    """``apps/<app>/frontend/src/appConfig.jsx`` 的 Connection 頁欄位名稱。"""
    text = (APPS / app / "frontend" / "src" / "appConfig.jsx").read_text(encoding="utf-8")
    start = text.index("\n  connection: {")
    return set(re.findall(r"\{ name: '(\w+)'", text[start:text.index("\n  },", start)]))


def assert_unknown_field_rejected(response, field: str, *submitted: str) -> None:
    """拼錯／沒宣告的欄位：422、指出是哪個欄位，而且不回顯任何送出的值（密碼、API key）。"""
    assert response.status_code == 422, response.text
    assert {"type": "extra_forbidden", "loc": ["body", field], "msg": "Extra inputs are not permitted"} in (
        response.json()["detail"]), response.text
    for value in submitted:
        assert value not in response.text


def product_admin(product: str, config_path: Path, monkeypatch):
    """``<product>_mcp_admin.main.app``（create_app 組裝，含 core 的 422 處理）的已登入 TestClient 與子行程套用的 AsyncMock。

    設定寫在 *config_path*（共用預設值）；只換掉連線 router 的子行程生命週期：不進 lifespan、不啟動子行程。
    """
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient

    app = importlib.import_module(f"{product}_mcp_admin.main").app
    router = importlib.import_module(f"{product}_mcp_admin.routers.config")
    config_path.write_text(json.dumps(store_mod._DEFAULT_CONFIG))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    apply = AsyncMock(return_value=True)
    monkeypatch.setattr(router, "get_process_manager", lambda: SimpleNamespace(
        is_running=True, restart=apply, start=apply, start_or_restart=apply))
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
    return client, apply
