"""mcp_admin_core 回歸測試網的共用 fixtures（Phase 0 安全網）。

重點：`JWT_SECRET` 必須在任何 mcp_admin_core 模組被 import 之前固定，
因為 auth/middleware.py 在 import 階段就讀取它。conftest.py 會在
pytest 收集任何測試模組之前載入，因此這裡是設定它最安全的位置。
"""

from __future__ import annotations

import os

# 必須最先設定 —— 在 import mcp_admin_core 之前固定 JWT_SECRET，
# 讓測試產生的 token 在整個 session 內可被驗證（否則每次 import 會隨機產生）。
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("JWT_EXPIRY_HOURS", "24")

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    """指向暫存 config.json 的路徑（檔案尚未建立）。"""
    return tmp_path / "config.json"


@pytest.fixture
def fresh_store(tmp_config_path: Path):
    """以暫存路徑建立的全新 ConfigStore（不碰 singleton）。"""
    from mcp_admin_core.config.store import ConfigStore

    return ConfigStore(path=tmp_config_path)


@pytest.fixture
def store_env(tmp_config_path: Path, monkeypatch):
    """把 get_config_store() singleton 指向暫存 config 並重建；測試後自動還原。

    用於需要透過 get_config_store() singleton 的元件（login / proxy / process）。
    """
    import mcp_admin_core.config.store as store_mod

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    return store_mod.get_config_store()


# hermes 測試共用的假主機名（hermes-agent-svc、gw、new-gw），測試環境解析不到。hermes 後台儲存／測試連線會做 SSRF 檢查（AI-HMCP-002-1），
# 這個主機名視為公開位址；其他網址照實檢查（SSRF 規則本身見 test_ssrf.py、test_hermes_security_guards.py）。
_SSRF_FIXTURE_PUBLIC_HOSTS = frozenset({"hermes-agent-svc", "gw", "new-gw"})


@pytest.fixture(autouse=True)
def _fixture_hosts_pass_ssrf(monkeypatch):
    from urllib.parse import urlsplit

    from mcp_admin_core.util import ssrf

    real_check_url = ssrf.check_url

    async def check_url(url, mode=None):
        try:
            host = urlsplit(url.strip()).hostname
        except (AttributeError, ValueError):
            host = None
        if host in _SSRF_FIXTURE_PUBLIC_HOSTS:
            return ssrf.SsrfCheck(True, "", ["203.0.113.10"])
        return await real_check_url(url, mode)

    monkeypatch.setattr(ssrf, "check_url", check_url)


@pytest.fixture
def make_app(tmp_config_path: Path, monkeypatch):
    """工廠 fixture：以指定的 config 覆寫值建立 app 並回傳 TestClient。

    config 覆寫值會先寫入暫存檔，再讓 get_config_store() singleton 以該檔重建，
    確保 store 的 asyncio.Lock 只在 TestClient 的事件圈內首次使用（避免跨圈綁定）。
    """
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient

    def _make(config_overrides: dict | None = None) -> TestClient:
        full = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        if config_overrides:
            full.update(config_overrides)
        tmp_config_path.write_text(json.dumps(full))
        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        store_mod.get_config_store()

        from mcp_admin_core import create_app

        # 不以 context manager 使用 TestClient，避免觸發 lifespan（不需啟動子行程）。
        return TestClient(create_app(title="Test Admin"))

    return _make


@pytest.fixture(autouse=True)
def _reset_launch_policy():
    """各 app 的 main 在 import 時以 create_app(launch_policy=...) 設定全域政策（#1530）；
    core 測試預設不帶政策，需要時自行 set_launch_policy。"""
    from mcp_admin_core.launch_policy import get_launch_policy, set_launch_policy

    saved = get_launch_policy()
    set_launch_policy(None)
    yield
    set_launch_policy(saved)
