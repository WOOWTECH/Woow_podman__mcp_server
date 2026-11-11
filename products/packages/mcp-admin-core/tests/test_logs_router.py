"""logs factory 契約測試：/search、SSE connected 事件、install idempotency。"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient


def _client_with_logs(logger_name: str) -> TestClient:
    from fastapi import FastAPI
    from mcp_admin_core.routers.logs import create_logs_router

    app = FastAPI()
    app.include_router(create_logs_router(logger_name))
    return TestClient(app)


def test_search_returns_buffer_matches():
    from mcp_admin_core.routers import logs as logs_mod

    c = _client_with_logs("phase1b_test_logs")
    logging.getLogger("phase1b_test_logs").info("hello-needle-123")
    r = c.get("/api/logs/search", params={"q": "needle"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "needle"
    assert any("needle" in e["message"] for e in body["entries"])


def test_invalid_regex_returns_400():
    c = _client_with_logs("phase1b_test_logs2")
    r = c.get("/api/logs/search", params={"q": "(", "regex": "true"})
    assert r.status_code == 400


def test_stream_first_event_is_connected():
    # 直接驅動 SSE producer 確認首個事件為 connected。
    # （Starlette 1.3.1 + httpx 0.28 的 TestClient 對無限 StreamingResponse
    #  generator 會在 c.stream() 進入時掛住——其棄用警告已標示 httpx testclient
    #  即將被 httpx2 取代——故改以直接 drive async generator 驗證同一契約。）
    import asyncio

    from mcp_admin_core.routers.logs import _sse_log_generator

    async def _first_event() -> str:
        gen = _sse_log_generator(tail_lines=1)
        try:
            return await asyncio.wait_for(gen.__anext__(), timeout=5)
        finally:
            await gen.aclose()

    first = asyncio.run(_first_event())
    assert "event: connected" in first


def test_install_is_idempotent():
    from mcp_admin_core.routers import logs as logs_mod

    logger = logging.getLogger("phase1b_idem")
    create = logs_mod.create_logs_router
    create("phase1b_idem")
    create("phase1b_idem")
    admin_handlers = [h for h in logger.handlers if type(h).__name__ == "_AdminLogHandler"]
    assert len(admin_handlers) == 1
