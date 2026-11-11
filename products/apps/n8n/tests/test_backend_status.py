"""n8n MCP 沒存後端連線時是文件模式：子行程照樣在跑，只列出 7 支文件工具、21 支管理工具都不出現（第五輪，負責人 2026-10-27）。

上游 n8n-mcp 2.96.2 的 ``getN8nApiConfig()`` 要 N8N_API_URL（合法網址）與 N8N_API_KEY（非空）兩個都在才回設定，
``tools/list`` 依 ``isN8nApiConfigured()`` 決定要不要列管理工具（tests/fixtures 的 server.js）。所以 API 網址與 API key 是後端
連線欄位：缺一個 ``/healthz`` 的 ``backend`` 就是 ``missing``；選填的 Native MCP Access Token 與 session 兩欄不算。``mcp`` 不變。
"""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mcp_admin_core.config.store as store_mod
import mcp_admin_core.process as process_mod
from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

from n8n_mcp_admin.main import LAUNCH_POLICY, app

FRONTEND_CONFIG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "appConfig.jsx"
UPSTREAM_SERVER = Path(__file__).with_name("fixtures") / "n8n-mcp-2.96.2-server.js.gz"
URL = {"n8n_api_url": "https://n8n.example.com"}
KEY = {"n8n_api_key": "n8n-api-key-SECRET-0123"}
OPTIONAL = {"n8n_mcp_access_token": "native-token-SECRET-0123", "mcp_session_timeout": 900, "mcp_max_sessions": 20}


class _RunningPM:
    is_running = True


@pytest.fixture
def healthz(tmp_path, monkeypatch):
    """``healthz(connection=..., running=..., raw=...)`` → 真的 n8n app 的 /healthz 回應（不進 lifespan、不 spawn 子行程）。"""
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


def test_backend_fields_are_the_api_url_and_key_with_their_page_labels():
    labels = dict(re.findall(r"\{ name: '(\w+)', label: '([^']+)'", FRONTEND_CONFIG.read_text(encoding="utf-8")))
    assert LAUNCH_POLICY.backend_connection == tuple((key, labels[key]) for key in ("n8n_api_url", "n8n_api_key"))
    assert LAUNCH_POLICY.required_connection == ()  # 文件模式照樣啟動：不是必要欄位


def test_upstream_lists_management_tools_only_when_the_api_is_configured():
    """上游 tools/list 以 isN8nApiConfigured()（N8N_API_URL 與 N8N_API_KEY 都有）決定要不要列 21 支管理工具。"""
    source = gzip.decompress(UPSTREAM_SERVER.read_bytes()).decode("utf-8")
    assert "const hasEnvConfig = (0, n8n_api_1.isN8nApiConfigured)();" in source
    assert "const shouldIncludeManagementTools = hasEnvConfig || hasInstanceConfig || isMultiTenantEnabled;" in source


@pytest.mark.parametrize(
    "connection",
    [None, {}, {"n8n_api_url": "", "n8n_api_key": ""}, URL, KEY, OPTIONAL, {**URL, **OPTIONAL}, {**URL, "n8n_api_key": " "}],
    ids=["no_section", "empty", "seeded_blank", "url_only", "key_only", "optional_only", "url_and_optional", "blank_key"],
)
def test_documentation_mode_reports_backend_missing(healthz, connection):
    r = healthz(connection)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "running", "backend": "missing"}


@pytest.mark.parametrize("connection", [{**URL, **KEY}, {**URL, **KEY, **OPTIONAL}], ids=["url_and_key", "with_optional"])
@pytest.mark.parametrize(("running", "mcp"), [(True, "running"), (False, "not_running")])
def test_api_url_and_key_mean_backend_configured_and_mcp_is_unchanged(healthz, connection, running, mcp):
    r = healthz(connection, running=running)
    assert r.json() == {"status": "ok", "mcp": mcp, "backend": "configured"}
    assert KEY["n8n_api_key"] not in r.text and "n8n_api_url" not in r.text


def test_unreadable_config_leaves_backend_out(healthz):
    r = healthz(raw="{broken", running=True)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mcp": "running"}
