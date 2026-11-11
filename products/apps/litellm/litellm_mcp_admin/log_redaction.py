"""把 MCP 端點的 path token 從 admin log 裡遮掉（來源 repo FINDING-006 同類問題）。

woow_paas_platform#1614 起改由共用層 ``mcp_admin_core.log_redaction`` 統一處理（``create_app()`` 會安裝，
每個 app 都套用；另外也遮 Logs 頁 EventSource 的 ``?token=<JWT>``）。本模組只保留舊名稱給既有呼叫者。
"""
from __future__ import annotations

from mcp_admin_core.log_redaction import PathTokenRedactionFilter, install, redact

__all__ = ["PathTokenRedactionFilter", "install", "redact"]
