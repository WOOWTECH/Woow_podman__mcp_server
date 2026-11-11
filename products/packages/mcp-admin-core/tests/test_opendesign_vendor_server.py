"""vendored od_mcp_server 回歸保護：import 不卡死＋常數/工具面對齊上游 d6d157a。

供 supergateway 以 `python -m opendesign_mcp_server` 橋接 stdio。本測試的核心價值是
「import 面守衛」：若未來 re-sync 上游時不慎拿掉 `if __name__ == "__main__":` 守衛，
模組頂層就會直接呼叫 `mcp.run(transport="stdio")` 並阻塞在 stdin ── 屆時光是 import
本模組（收集階段即發生）就會讓整個 pytest 掛死，藉此把回歸暴露出來。
"""
from __future__ import annotations

# 頂層 import：若上游 run guard 掉了，這行會阻塞、整個測試會話掛死（即回歸訊號）。
from opendesign_mcp_server import od_mcp_server as srv


def test_module_imports_without_hanging():
    # 能執行到這裡，就證明 import 沒有阻塞（mcp.run 受 __main__ 守衛）。
    assert srv is not None


def test_has_fastmcp_instance():
    assert hasattr(srv, "mcp")
    # MCPServer 實例（mcp 2.x；1.x 叫 FastMCP，vendored 檔的 import 由套件 __init__ 暫時別名，#78）。
    assert srv.mcp.__class__.__name__ == "MCPServer"


def test_od_api_base_default():
    # 上游模組頂層固定讀 OD_API_BASE，預設對齊 daemon service 位址。
    assert srv.OD_API_BASE == "http://open-design-svc:7457"


def test_chat_timeout_default():
    # 長時 AI 操作預設等待秒數，對映 OD_CHAT_TIMEOUT。
    assert srv.CHAT_TIMEOUT == 300.0


def test_fifteen_tools_registered():
    # 15 支 @mcp.tool 忠實帶入；list_tools 是 async，故用 anyio 跑一次取數。
    import anyio

    tools = anyio.run(srv.mcp.list_tools)
    names = {t.name for t in tools}
    assert len(names) == 15
    assert {"health", "create_project", "delete_project", "send_message"} <= names
