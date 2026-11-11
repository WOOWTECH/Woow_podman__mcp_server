"""``woow-odoo-mcp``：先套用四個執行期修補，再啟動上游 ``odoo-mcp``。

MCP 子行程的 command 由 LaunchPolicy 固定（見 ``main.py``）；這個入口與上游 ``odoo-mcp`` 的參數、
環境變數完全相同，只在啟動前：

0. ``upstream_fixes``：資料讀取不再吞掉錯誤、回「成功但沒有資料」（見該檔）；
1. ``result_limits``：search_records／read_record 結果超過 ``ODOO_MCP_MAX_RESULT_BYTES``（預設 2 MiB）
   時改回可讀的失敗（DATA-OMCP-007-3：3 筆附件 datas 就 17.7 MiB）；
2. ``tool_errors``：工具回傳 ``success: false`` 時改回 ``isError=true``（DATA-OMCP-004-4／007-2）；
   另以 core 的 ``keep_error_text`` 讓工具直接丟出的例外照 mcp 1.x 回「Error executing tool X: 原因」
   （mcp 2.x 預設只回 ``Error executing tool X``，#78）；
3. ``concurrency``：唯讀工具改在工作緒執行，多個 session 並發時不互相卡住（DATA-ODOO-011-1）；
   寫入類維持在事件迴圈上序列執行（mcp 2.x 預設會把同步工具全部丟工作緒並行，#78）。

順序固定：先量大小、再包錯誤旗標與例外文字、最後包工作緒（大小檢查因此也在工作緒裡跑）。

另外把 Streamable HTTP／SSE 的 ``max_request_body_size`` 設成 PaaS 上限（``mcp_admin_core.request_body``，16 MiB）：
MCP SDK 預設 4 MiB，超過就在處理前回 413，擋下大型工具參數（例如 create／write 帶附件 datas）。mcp 2.x 的
``MCPServer`` 已沒有 ``settings.max_request_body_size``（transport 參數改由 ``run()`` 傳入），所以由
``request_body.apply_to_run`` 補進上游 ``main`` 呼叫的 ``mcp.run(...)``。
"""

from __future__ import annotations

from . import transport_safety


def main() -> None:
    # Importing transport_safety also imports upstream server for tool registration.
    # The pinned wheel constructs no OdooClient during import (tested separately).
    # Install before the first client is constructed; the stdlib transport would
    # otherwise replay an ambiguous XML-RPC write once.
    transport_safety.install()
    from mcp_admin_core import request_body
    from mcp_admin_core.tool_error_text import keep_error_text
    from odoo_mcp.__main__ import (
        main as upstream_main,  # 會 import odoo_mcp.server（工具在此註冊）
    )
    from odoo_mcp.odoo_client import OdooClient
    from odoo_mcp.server import mcp

    from . import concurrency, result_limits, tool_errors, upstream_fixes

    # Streamable HTTP 與 SSE 都從 run() 的參數拿上限（上游 main 只傳 host／port／path／transport_security）。
    request_body.apply_to_run(mcp)
    upstream_fixes.propagate_read_errors(OdooClient)
    result_limits.install(mcp)
    tool_errors.install(mcp)
    keep_error_text(mcp)
    concurrency.install(mcp, OdooClient)
    upstream_main()


if __name__ == "__main__":
    main()
