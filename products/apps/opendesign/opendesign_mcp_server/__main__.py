"""`python -m opendesign_mcp_server` 進入點：stdio 模式（相容／除錯用）。

正式部署改用原生 HTTP launcher（console script ``opendesign-mcp-server``，見 ``launcher.py``，
#1369）；舊 supergateway seed 由 admin 啟動時自動遷移。保留 stdio 入口供本機以 MCP inspector
等 stdio client 直接除錯。保持 vendored 的 od_mcp_server.py 盡量乾淨，故把啟動放這層薄 wrapper。
"""
from opendesign_mcp_server.od_mcp_server import mcp

if __name__ == "__main__":
    mcp.run(transport="stdio")
