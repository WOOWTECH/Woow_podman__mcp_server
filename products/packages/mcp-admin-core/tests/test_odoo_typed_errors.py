"""Pydantic 讀取結果失敗時，MCP 錯誤旗標與 outputSchema 必須一致。"""

import json

import pytest

pytest.importorskip("odoo_mcp")

from mcp.types import CallToolResult
from odoo_mcp.tool_helpers import SearchEmployeeResponse, SearchHolidaysResponse
from odoo_mcp_admin.tool_errors import as_error_result


@pytest.mark.parametrize("response_type", [SearchEmployeeResponse, SearchHolidaysResponse])
def test_typed_failure_is_visible_to_mcp_clients(response_type):
    result = as_error_result(response_type(success=False, error="Access denied"))
    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert result.structured_content == json.loads(result.content[0].text)
    assert result.structured_content == {
        "success": False, "result": None, "error": "Access denied",
    }


@pytest.mark.parametrize("response_type", [SearchEmployeeResponse, SearchHolidaysResponse])
def test_typed_success_still_uses_normal_sdk_conversion(response_type):
    result = response_type(success=True, result=[])
    assert as_error_result(result) is result


def test_typed_failure_preserves_wrapped_schema():
    result = as_error_result(SearchHolidaysResponse(success=False, error="missing model"), wrap=True)
    assert result.structured_content == {"result": json.loads(result.content[0].text)}


def test_typed_traceback_is_scrubbed():
    result = as_error_result(SearchHolidaysResponse(
        success=False,
        error="Traceback (most recent call last):\n  File '/private/path.py', line 4\nAccessError: denied\n",
    ))
    assert result.structured_content["error"] == "Odoo server error: AccessError: denied"
    assert "/private/" not in result.content[0].text


async def test_real_sdk_returns_error_for_typed_holiday_failure(monkeypatch):
    from mcp.client import Client
    from odoo_mcp import server, tools_read
    from odoo_mcp_admin import tool_errors

    class MissingHolidayModel:
        def search_read(self, **kwargs):
            raise RuntimeError("Object hr.leave.report.calendar doesn't exist")

    monkeypatch.setattr(tools_read, "_resolve_odoo", lambda *_: ("default", MissingHolidayModel()))
    originals = {t.name: t.fn for t in server.mcp._tool_manager.list_tools()}
    try:
        tool_errors.install(server.mcp)
        async with Client(server.mcp) as client:
            result = await client.call_tool("search_holidays", {
                "start_date": "2026-11-01", "end_date": "2026-11-02",
            })
        assert result.is_error is True
        assert result.structured_content["success"] is False
        assert "hr.leave.report.calendar" in result.structured_content["error"]
    finally:
        for tool in server.mcp._tool_manager.list_tools():
            tool.fn = originals[tool.name]
