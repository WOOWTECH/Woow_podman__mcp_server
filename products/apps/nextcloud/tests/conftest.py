"""Nextcloud MCP Admin 測試共用 fixture。

沒有任何測試連真的 Nextcloud：後端一律是 ``tests/fake_nextcloud.py``（httpx.MockTransport）。
``make_client`` 以 core app factory 組 app、寫暫存 config.json、回傳已登入的 TestClient（同 apps/litellm）。
"""

from __future__ import annotations

import os

# core 的 auth middleware 在 import 時就讀 JWT_SECRET；必須在任何 mcp_admin_core 模組被 import 之前固定。
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("JWT_EXPIRY_HOURS", "24")

import base64
import json
import sys

import httpx
import pytest

from .fake_nextcloud import BASE_URL, LOGIN, PASSWORD, FakeNextcloud


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """不讓外部 NEXTCLOUD_MCP_* env、.env 檔或 backend_policy 模組滲入（vendored Settings 會讀 env／.env）。"""
    for key in list(os.environ):
        if key.upper().startswith("NEXTCLOUD_MCP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "backend_policy", raising=False)
    # vendored client 的 401／429 鎖存是整個 process 共用（依帳密分鍵）；測試間清掉，免得前一個測試的錯密碼影響下一個。
    from nextcloud_mcp_server.client import reset_auth_latch

    reset_auth_latch()
    yield
    reset_auth_latch()


@pytest.fixture
def fake() -> FakeNextcloud:
    nc = FakeNextcloud()
    nc.add_folder("Docs")
    nc.add_file("Docs/readme.md", b"# Hello\n", "text/markdown")
    nc.add_file("Docs/a #1.md", b"hash name", "text/markdown")
    nc.add_folder("Docs/Sub")
    nc.add_file("Docs/Sub/deep.txt", b"deep", "text/plain")
    nc.add_file("photo.png", b"\x89PNG\r\n\x1a\n\x00\x00", "image/png")
    nc.add_folder("Empty")
    nc.add_calendar("personal", "Personal", ["VEVENT", "VTODO"])
    return nc


class AuthCheckingNextcloud:
    """在 FakeNextcloud 前面驗 HTTP Basic：帳密不符回 401（fake 本身不驗）；/status.php 免認證。"""

    def __init__(self, fake: FakeNextcloud, *, username: str = LOGIN, password: str = PASSWORD,
                 version: str | None = "35.0.1") -> None:
        self.fake = fake
        self.expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.version = version
        self.auth_headers: list[str | None] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status.php"):
            if self.version is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"installed": True, "versionstring": self.version,
                                             "version": self.version + ".2"})
        self.auth_headers.append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") != self.expected:
            return httpx.Response(401)
        return self.fake.handle(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


@pytest.fixture
def authed(fake) -> AuthCheckingNextcloud:
    return AuthCheckingNextcloud(fake)


# ---------------------------------------------------------------------------
# monorepo admin 測試：core app factory + 暫存 config.json + 已登入 TestClient。
# ---------------------------------------------------------------------------
@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "config.json"


@pytest.fixture
def make_client(config_path, monkeypatch):
    """工廠：``make_client(connection=..., mcp_server=..., tools=..., app=...)`` → 已登入 TestClient。

    預設 app 只掛 nextcloud 的 config router；傳 ``app="main"`` 則用完整的
    ``nextcloud_mcp_admin.main.app``（不進 lifespan，不會 spawn 子行程）。
    """
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod

    def _make(connection=None, mcp_server=None, tools=None, app=None):
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        if connection is not None:
            cfg["connection"] = connection
        if mcp_server is not None:
            cfg["mcp_server"] = {**cfg["mcp_server"], **mcp_server}
        if tools is not None:
            cfg["tools"] = tools
        config_path.write_text(json.dumps(cfg))

        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        monkeypatch.setattr(process_mod, "_instance", None)
        store_mod.get_config_store()

        from fastapi.testclient import TestClient

        if app == "main":
            from nextcloud_mcp_admin.main import app as the_app
        else:
            from mcp_admin_core.app import create_app

            from nextcloud_mcp_admin.routers import config as config_router

            the_app = create_app(title="Nextcloud Test Admin", extra_routers=[config_router.router])
        client = TestClient(the_app)
        token = client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        return client

    return _make


def stored_config(config_path) -> dict:
    return json.loads(config_path.read_text())


CONN = {"base_url": BASE_URL, "username": LOGIN, "app_password": PASSWORD}
