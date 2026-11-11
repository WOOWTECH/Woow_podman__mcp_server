"""Shared fixtures for the LiteLLM MCP server test suite.

（移植自 WOOWTECH/Woow_litellm_mcp_server@4d41903 的 tests/conftest.py；另加 monorepo admin
測試用的 ``make_client``——以 core app factory 組 app、寫暫存 config.json、回傳已登入的
TestClient。）

Nothing here talks to a real LiteLLM gateway. Tool HTTP calls are intercepted
with ``httpx.MockTransport`` so we can assert on the exact request each tool
builds (method, path, query params, JSON body) and control the response the
tool parses. The MCP layer is exercised through a tiny ``FakeMCP`` that captures
the real tool coroutines each ``register()`` installs, so the production tool
code runs unchanged.
"""

from __future__ import annotations

import os

# core 的 auth middleware 在 import 時就讀 JWT_SECRET；必須在任何 mcp_admin_core 模組被
# import 之前固定（比照 packages/mcp-admin-core 與 apps/k8s 的 conftest）。
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("JWT_EXPIRY_HOURS", "24")

import json
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from woow_litellm_mcp_server.gating import ToolGate
from woow_litellm_mcp_server.lifespan import LITELLM_CLIENT_KEY
from woow_litellm_mcp_server.settings import Settings
from woow_litellm_mcp_server.tools import MODULES


# ---------------------------------------------------------------------------
# A minimal stand-in for FastMCP that records the tools a module registers.
# ---------------------------------------------------------------------------
class FakeMCP:
    """Captures ``@mcp.tool(...)``-decorated coroutines by name."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}
        self.annotations: dict[str, Any] = {}

    def tool(self, *args: Any, name: str | None = None, annotations: Any = None, **_: Any):
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            key = name or getattr(fn, "__name__", None)
            assert key, "tool must have a name"
            self.tools[key] = fn
            self.annotations[key] = annotations
            return fn

        # Support both `@mcp.tool` and `@mcp.tool(...)` usage.
        if args and callable(args[0]) and name is None:
            return decorator(args[0])
        return decorator


def build_gate(**overrides: Any) -> ToolGate:
    """A gate built from explicit overrides (init kwargs beat any env)."""
    return ToolGate(
        readonly=overrides.get("readonly", False),
        disabled_categories=overrides.get("disabled_categories", []),
        disabled_tools=overrides.get("disabled_tools", []),
        disabled_operations=overrides.get("disabled_operations", []),
    )


def register_all(gate: ToolGate) -> FakeMCP:
    """Register every tool module against a FakeMCP with the given gate."""
    mcp = FakeMCP()
    for module in MODULES:
        module.register(mcp, gate)
    return mcp


@pytest.fixture
def all_enabled_gate() -> ToolGate:
    return build_gate()


@pytest.fixture
def registered(all_enabled_gate: ToolGate) -> FakeMCP:
    """A FakeMCP with every gate-enabled tool registered."""
    return register_all(all_enabled_gate)


# ---------------------------------------------------------------------------
# A recording httpx transport standing in for the LiteLLM gateway.
# ---------------------------------------------------------------------------
class MockLiteLLM:
    """Records outbound requests and returns canned JSON responses."""

    def __init__(self, base_url: str = "http://litellm.test") -> None:
        self.base_url = base_url
        self.requests: list[httpx.Request] = []
        self._routes: dict[str, tuple[int, Any]] = {}
        self._default: tuple[int, Any] = (200, {})

    def route(self, path: str, payload: Any, status: int = 200) -> "MockLiteLLM":
        self._routes[path] = (status, payload)
        return self

    def default(self, payload: Any, status: int = 200) -> "MockLiteLLM":
        self._default = (status, payload)
        return self

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, payload = self._routes.get(request.url.path, self._default)
        return httpx.Response(status, json=payload)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            transport=httpx.MockTransport(self._handler),
        )

    # -- convenience accessors for assertions ------------------------------
    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no request was made"
        return self.requests[-1]

    def last_json(self) -> Any:
        body = self.last.content
        return json.loads(body) if body else None

    def last_params(self) -> dict[str, str]:
        return dict(self.last.url.params)


def make_ctx(client: httpx.AsyncClient) -> SimpleNamespace:
    """A fake FastMCP Context exposing the lifespan context dict."""
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={LITELLM_CLIENT_KEY: client}
        )
    )


@pytest.fixture
def mock_litellm() -> MockLiteLLM:
    return MockLiteLLM()


@pytest.fixture
async def ctx(mock_litellm: MockLiteLLM):
    """A ready-to-use fake Context whose client is the mock gateway."""
    client = mock_litellm.client()
    try:
        yield make_ctx(client)
    finally:
        await client.aclose()


@pytest.fixture
def settings_factory():
    """Build a Settings object with explicit values (init kwargs beat env)."""

    def _make(**kwargs: Any) -> Settings:
        return Settings(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# Temp config store: point mcp_admin_core + the admin routers at a tmp file and
# reset their module-level singletons so tests never touch /data/config.json.
# ---------------------------------------------------------------------------
@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(cfg_path))

    # Reset the cached singletons so they pick up the temp path.
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod

    monkeypatch.setattr(store_mod, "_instance", None, raising=False)
    monkeypatch.setattr(process_mod, "_instance", None, raising=False)

    yield cfg_path

    monkeypatch.setattr(store_mod, "_instance", None, raising=False)
    monkeypatch.setattr(process_mod, "_instance", None, raising=False)


# ---------------------------------------------------------------------------
# monorepo admin 測試：core app factory + 暫存 config.json + 已登入 TestClient。
# ---------------------------------------------------------------------------
@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "config.json"


@pytest.fixture
def make_client(config_path, monkeypatch):
    """工廠：``make_client(connection=..., mcp_server=..., app=...)`` → 已登入 TestClient。

    預設 app 只掛 litellm 的 config router；傳 ``app="main"`` 則用完整的
    ``litellm_mcp_admin.main.app``（不進 lifespan，不會 spawn 子行程）。
    """
    import mcp_admin_core.config.store as store_mod
    import mcp_admin_core.process as process_mod

    def _make(connection=None, mcp_server=None, app=None):
        cfg = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
        if connection is not None:
            cfg["connection"] = connection
        if mcp_server is not None:
            cfg["mcp_server"] = {**cfg["mcp_server"], **mcp_server}
        config_path.write_text(json.dumps(cfg))

        monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))
        monkeypatch.setattr(store_mod, "_instance", None)
        monkeypatch.setattr(process_mod, "_instance", None)
        store_mod.get_config_store()

        from fastapi.testclient import TestClient

        if app == "main":
            from litellm_mcp_admin.main import app as the_app
        else:
            from mcp_admin_core.app import create_app
            from litellm_mcp_admin.routers import config as config_router

            the_app = create_app(title="LiteLLM Test Admin", extra_routers=[config_router.router])
        client = TestClient(the_app)
        token = client.post("/api/auth/login", json={"password": "admin"}).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        return client

    return _make


def stored_config(config_path) -> dict:
    return json.loads(config_path.read_text())
