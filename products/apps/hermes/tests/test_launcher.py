"""Hermes MCP server 雙 transport launcher 的契約測試（cloud-service）。

對外契約：MCP client 經 admin reverse proxy 打 ``/private_{token}/{sse,mcp}``，
proxy strip ``/private_{token}/`` 前綴後反代到 ``127.0.0.1:{mcp_server.port}/{sse,mcp}``
（mcp-admin-core proxy.py，預設 port 8000）。故被 spawn 的 hermes 子行程須經由
core 共用的 :class:`~mcp_admin_core.mcp_sse_wrapper.DualTransportApp` 同時暴露：
  - SSE            ：``/sse``（GET 串流）+ ``/messages/``（POST 回傳通道）  ← 既有 client
  - StreamableHTTP ：``/mcp``                                          ← MCP 2025-03 client
  - 預設綁 loopback ``127.0.0.1:8000``（與 config ``mcp_server.port`` 預設對齊）
"""
from __future__ import annotations

from starlette.routing import Mount, Route


def test_server_exposes_low_level_server() -> None:
    """server 模組須暴露 low-level Server 供 DualTransportApp 掛雙 transport（對齊 odoo 命名）。"""
    from mcp.server.lowlevel import Server

    from hermes_mcp_server.server import server

    assert isinstance(server, Server)


def test_dual_transport_app_exposes_sse_and_messages_routes() -> None:
    """DualTransportApp 須掛出 /sse 與 /messages/ 路由（既有 SSE client 端點）。"""
    from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

    from hermes_mcp_server.server import server

    app = DualTransportApp(server)
    paths = {r.path for r in app.routes if isinstance(r, (Route, Mount))}
    assert "/sse" in paths
    assert any(p.startswith("/messages") for p in paths)


def test_launcher_starts_dual_transport_app_on_proxy_contract(monkeypatch) -> None:
    """launcher.main 須以 DualTransportApp 啟動（雙 transport），預設綁 127.0.0.1:8000。"""
    import hermes_mcp_server.launcher as launcher
    from mcp_admin_core.mcp_sse_wrapper import DualTransportApp

    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(launcher.uvicorn, "run", fake_run)
    launcher.main([])

    assert isinstance(captured["app"], DualTransportApp)
    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["kwargs"]["port"] == 8000


def test_launcher_main_is_importable_and_callable() -> None:
    """launcher.main 是可作為 console script (hermes-mcp-server) 的進入點。"""
    from hermes_mcp_server.launcher import main

    assert callable(main)
