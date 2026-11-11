"""Hermes MCP 沒存後端連線時：子行程照樣在跑、列出 11 支工具，但工具回「… is not configured」（第五輪，負責人 2026-10-27）。

10 支工具要 Dashboard（網址＋帳密登入），hermes_chat 與 hermes_inspect 要 Gateway（網址＋API Key；PaaS 的 Hermes Agent 兩邊都
要認證）。五個欄位都是後端連線欄位：有一個沒填，``/healthz`` 的 ``backend`` 就是 ``missing``；``mcp`` 照舊只看子行程。
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

from hermes_mcp_admin.main import LAUNCH_POLICY, app

FRONTEND_CONFIG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "appConfig.jsx"
GATEWAY = {"gateway_url": "https://hermes.example.com/gateway", "gateway_api_key": "gateway-key-SECRET-0123"}
DASHBOARD = {"dashboard_url": "https://hermes.example.com", "dashboard_username": "admin",
             "dashboard_password": "dashboard-pass-SECRET-0123"}


class _RunningPM:
    is_running = True


@pytest.fixture
def healthz(tmp_path, monkeypatch):
    """``healthz(connection=..., running=..., raw=...)`` → 真的 hermes app 的 /healthz 回應（不進 lifespan、不 spawn 子行程）。"""
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


def test_backend_fields_are_the_five_connection_fields_with_their_page_labels():
    labels = dict(re.findall(r"\{ name: '(\w+)', label: '([^']+)'", FRONTEND_CONFIG.read_text(encoding="utf-8")))
    assert LAUNCH_POLICY.backend_connection == tuple((key, labels[key]) for key in (*GATEWAY, *DASHBOARD))
    assert LAUNCH_POLICY.required_connection == ()


@pytest.mark.parametrize(
    "connection",
    [
        None,
        {},
        dict.fromkeys((*GATEWAY, *DASHBOARD), ""),  # chart seed 的空鍵
        GATEWAY,  # 只設 Gateway：10 支要 Dashboard 的工具失敗
        DASHBOARD,  # 只設 Dashboard：hermes_chat 失敗
        {"gateway_url": GATEWAY["gateway_url"], "dashboard_url": DASHBOARD["dashboard_url"]},  # 只有網址、沒有憑證
        {**GATEWAY, **DASHBOARD, "dashboard_password": " "},
    ],
    ids=["no_section", "empty", "seeded_blank", "gateway_only", "dashboard_only", "urls_only", "blank_password"],
)
def test_missing_any_field_means_backend_missing(healthz, connection):
    r = healthz(connection)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "running", "backend": "missing"}


@pytest.mark.parametrize(("running", "mcp"), [(True, "running"), (False, "not_running")])
def test_all_five_fields_mean_backend_configured_and_mcp_is_unchanged(healthz, running, mcp):
    r = healthz({**GATEWAY, **DASHBOARD}, running=running)
    assert r.json() == {"status": "ok", "mcp": mcp, "backend": "configured"}
    for secret in (GATEWAY["gateway_api_key"], DASHBOARD["dashboard_password"], "gateway_url"):
        assert secret not in r.text


def test_unreadable_config_leaves_backend_out(healthz):
    r = healthz(raw="[]", running=True)  # 不是物件的設定＝讀不到
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "running"}
