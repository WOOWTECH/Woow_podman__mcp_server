"""Odoo MCP 沒存後端連線時：子行程照樣在跑、列出 41 支工具，但每支碰 Odoo 的工具都失敗（第五輪，負責人 2026-10-27）。

上游 odoo-mcp 1.3.0 只有在 ODOO_URL／ODOO_DB／ODOO_USERNAME／ODOO_PASSWORD 四個都在時才建 Odoo 連線，XML-RPC 的
authenticate 也要資料庫、帳號與密碼（API Key）。所以四個欄位都是後端連線欄位：有一個沒填，``/healthz`` 的 ``backend`` 就是
``missing``，平台顯示較輕的「尚未設定後端連線」提示；``mcp`` 照舊只看子行程（odoo 沒有必要欄位，不會是 not_configured）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mcp_admin_core.config.store as store_mod
import mcp_admin_core.process as process_mod
from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

from odoo_mcp_admin.main import LAUNCH_POLICY, app

FRONTEND_CONFIG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "appConfig.jsx"
FILLED = {"odoo_url": "https://odoo.example.com", "odoo_db": "prod", "odoo_username": "mcp-bot",
          "odoo_password": "odoo-api-key-SECRET-0123"}


class _RunningPM:
    is_running = True


@pytest.fixture
def healthz(tmp_path, monkeypatch):
    """``healthz(connection=..., running=..., raw=...)`` → 真的 odoo app 的 /healthz 回應（不進 lifespan、不 spawn 子行程）。"""
    previous = get_launch_policy()

    def _get(connection=None, *, running=True, raw=None):
        path = tmp_path / "config.json"
        if raw is not None:
            path.write_text(raw)
        else:
            cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
            if connection is not None:
                cfg["connection"] = connection
            path.write_text(json.dumps(cfg))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
        monkeypatch.setattr(store_mod, "_instance", None)
        monkeypatch.setattr(process_mod, "_instance", _RunningPM() if running else None)
        set_launch_policy(LAUNCH_POLICY)  # 別的測試以 create_app() 組 app 時會把全域政策換掉
        return TestClient(app).get("/healthz")

    yield _get
    set_launch_policy(previous)


def test_backend_fields_are_the_four_connection_fields_with_their_page_labels():
    labels = dict(re.findall(r"\{ name: '(\w+)', label: '([^']+)'", FRONTEND_CONFIG.read_text(encoding="utf-8")))
    assert LAUNCH_POLICY.backend_connection == tuple(
        (key, labels[key]) for key in ("odoo_url", "odoo_db", "odoo_username", "odoo_password"))
    assert LAUNCH_POLICY.required_connection == ()  # 沒有連線也起得來：不是必要欄位


@pytest.mark.parametrize("connection", [None, {}, dict.fromkeys(FILLED, "")], ids=["no_section", "empty", "seeded_blank"])
def test_fresh_service_reports_backend_missing(healthz, connection):
    """新開的服務（平台只 seed 管理密碼）：子行程在跑（mcp running），後端沒接上（backend missing）。"""
    r = healthz(connection)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "running", "backend": "missing"}


@pytest.mark.parametrize("blank", sorted(FILLED))
def test_any_blank_field_means_backend_missing(healthz, blank):
    assert healthz({**FILLED, blank: "  "}).json() == {"status": "ok", "mcp": "running", "backend": "missing"}
    assert healthz({k: v for k, v in FILLED.items() if k != blank}).json()["backend"] == "missing"


@pytest.mark.parametrize(("running", "mcp"), [(True, "running"), (False, "not_running")])
def test_all_four_fields_mean_backend_configured_and_mcp_is_unchanged(healthz, running, mcp):
    r = healthz(FILLED, running=running)
    assert r.json() == {"status": "ok", "mcp": mcp, "backend": "configured"}
    assert FILLED["odoo_password"] not in r.text and "odoo_url" not in r.text


def test_unreadable_config_leaves_backend_out(healthz):
    r = healthz(raw="{broken", running=False)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "not_running"}
