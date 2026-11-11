"""上游 ``odoo-mcp``（1.1.0 起，1.3.0 仍是）的資料讀取吞掉錯誤、回傳「成功但沒有資料」。

``OdooClient.search_read`` 與 ``OdooClient.read_records`` 把**所有**例外（連線中斷、逾時、
AccessError、不存在的 model、record rule 拒絕…）吞掉，只往 stderr 印一行，然後回 ``[]``。
上層工具（search_records、read_record、各種報表）於是回 ``{"success": true, "count": 0,
"result": []}``——呼叫端的代理只會認定「查無資料」：可能因此重複建立既有紀錄、報表少算、
或把權限不足誤判為沒有資料。商用驗收 DATA-FIX-GEN 的 fake-Odoo 實驗重現（Odoo 端回 Fault，
工具卻回 success: true／count: 0）；DATA-OMCP-003（ACL／record rule）還沒能實測，這個行為會
讓它的「錯誤要以 isError 回」判準無從成立。

修法：兩個方法改成讓例外往上拋。每個呼叫端（tools_read／tools_cross_instance／data_quality／
accounting_tools／server_core 的 resource）本來就在 ``try/except Exception`` 裡把例外轉成
``{"success": false, "error": ...}``，所以結果會是真正的錯誤回應（再由 ``tool_errors`` 標 isError）。
``get_installed_modules`` 是 best-effort 的資訊欄位（profile），維持原樣。
"""

from __future__ import annotations

from typing import Any


def propagate_read_errors(client_cls: Any) -> None:
    def search_read(
        self,
        model_name: str,
        domain: list,
        fields: list | None = None,
        offset: int | None = None,
        limit: int | None = None,
        order: str | None = None,
    ) -> list:
        kwargs: dict[str, Any] = {}
        if offset:
            kwargs["offset"] = offset
        if fields is not None:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        if order is not None:
            kwargs["order"] = order
        return self._execute(model_name, "search_read", domain, **kwargs)

    def read_records(self, model_name: str, ids: list, fields: list | None = None) -> list:
        kwargs: dict[str, Any] = {}
        if fields is not None:
            kwargs["fields"] = fields
        return self._execute(model_name, "read", ids, **kwargs)

    client_cls.search_read = search_read
    client_cls.read_records = read_records
