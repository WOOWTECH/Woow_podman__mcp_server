"""DATA-OMCP-007-2：Odoo MCP 的 /api/health 要分得出 DNS 失敗、拒絕連線、逾時。

修前 ``_check_odoo`` 把所有 ``httpx.ConnectError`` 都報成 ``"Connection refused"``——Odoo 網址打錯字
（DNS 查不到）也說「拒絕連線」，維運會往防火牆／Odoo 沒起來的方向查。這裡用真的 socket 打（不 mock），
確認 httpx 的例外鏈（httpx.ConnectError → httpcore.ConnectError → socket.gaierror／ConnectionRefusedError）
被正確辨識。``healthy``、``url`` 等欄位與整體狀態判斷不變。
"""

from __future__ import annotations

import socket

import httpx
import pytest


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_dns_failure_is_not_reported_as_connection_refused():
    from odoo_mcp_admin.health_probes import _check_odoo

    out = await _check_odoo("http://odoo-typo.invalid")
    assert out["healthy"] is False
    assert out["url"] == "http://odoo-typo.invalid"
    assert "refused" not in out["error"].lower()
    assert out["error"].startswith("DNS lookup failed")
    assert "odoo-typo.invalid" in out["error"]


async def test_refused_connection_still_says_connection_refused():
    from odoo_mcp_admin.health_probes import _check_odoo

    out = await _check_odoo(f"http://127.0.0.1:{_closed_port()}")
    assert out["healthy"] is False
    assert out["error"] == "Connection refused"


async def test_connect_timeout_is_reported_as_timeout(monkeypatch):
    from odoo_mcp_admin import health_probes as hp

    class _TimeoutClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def get(self, url, **kwargs):
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(hp.httpx, "AsyncClient", _TimeoutClient)
    out = await hp._check_odoo("http://10.255.255.1")
    assert out["healthy"] is False
    assert out["error"] == "Timed out"


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (socket.gaierror(-2, "Name or service not known"), "DNS lookup failed"),
        (ConnectionRefusedError(111, "Connect call failed"), "Connection refused"),
        (TimeoutError("timed out"), "Connection timed out"),
        (OSError(113, "No route to host"), "Connection failed"),
    ],
)
def test_describe_connect_error_walks_the_exception_chain(cause, expected):
    from mcp_admin_core.util.net_errors import describe_connect_error

    try:
        try:
            raise cause
        except OSError as inner:
            raise httpx.ConnectError(str(inner)) from inner
    except httpx.ConnectError as exc:
        text = describe_connect_error(exc, host="odoo.example")
    assert text.startswith(expected)
