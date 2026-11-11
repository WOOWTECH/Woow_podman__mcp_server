"""health_probes：healthy 只由帶 key 的 /v1/models 決定；readiness 只補細節。"""
from __future__ import annotations

import httpx
import pytest

from litellm_mcp_admin import health_probes

KEY = "sk-probe-key-abcdef"


@pytest.fixture(autouse=True)
def _clear_version_cache():
    health_probes._version_cache.clear()
    yield
    health_probes._version_cache.clear()


def _gateway(models_status=200, models_body=None, readiness_body=None, record=None, openapi_version="1.103.3"):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        if request.url.path == health_probes.OPENAPI_PATH:
            if openapi_version is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json={"openapi": "3.1.0", "info": {"title": "LiteLLM API", "version": openapi_version}})
        if request.url.path == health_probes.MODELS_PATH:
            return httpx.Response(models_status, json=models_body if models_body is not None else {"data": [{"id": "a"}, {"id": "b"}]})
        if request.url.path == health_probes.READINESS_PATH:
            return httpx.Response(200, json=readiness_body if readiness_body is not None else
                                  {"status": "healthy", "db": "connected", "litellm_version": "1.83.14"})
        return httpx.Response(404, json={"detail": "nope"})

    return httpx.MockTransport(handler)


async def test_healthy_with_key_sends_bearer_and_fills_details():
    seen: list[httpx.Request] = []
    result = await health_probes.check_gateway("http://lite:4000", KEY, transport=_gateway(record=seen))
    assert result["healthy"] is True and result["auth_ok"] is True
    assert result["model_count"] == 2
    assert result["version"] == "1.83.14" and result["db"] == "connected"
    models_req = next(r for r in seen if r.url.path == "/v1/models")
    assert models_req.headers["authorization"] == f"Bearer {KEY}"
    # readiness 是免認證端點：不要把金鑰送去
    ready_req = next(r for r in seen if r.url.path == "/health/readiness")
    assert "authorization" not in ready_req.headers
    assert KEY not in str(result)


async def test_rejected_key_is_not_healthy_even_if_readiness_ok():
    result = await health_probes.check_gateway("http://lite:4000", "sk-wrong", transport=_gateway(models_status=401))
    assert result["healthy"] is False and result["auth_ok"] is False
    assert "拒絕" in result["error"]
    assert result["version"] == "1.83.14"  # 細節照補


async def test_missing_key_message():
    result = await health_probes.check_gateway("http://lite:4000", "", transport=_gateway(models_status=401))
    assert result["healthy"] is False and "尚未設定" in result["error"]


async def test_not_found_hints_v1():
    result = await health_probes.check_gateway("http://lite:4000/v1", KEY, transport=_gateway(models_status=404))
    assert result["healthy"] is False and "/v1" in result["error"]


async def test_unreachable():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    result = await health_probes.check_gateway("http://lite:4000", KEY, transport=httpx.MockTransport(boom))
    assert result["healthy"] is False and "Connection refused" in result["error"]


async def test_not_configured():
    result = await health_probes.check_gateway("", KEY)
    assert result["healthy"] is False and "Not configured" in result["error"]


def test_connection_target_strips():
    assert health_probes.connection_target({"base_url": " http://lite:4000/ ", "master_key": f" {KEY} "}) == ("http://lite:4000", KEY)
    assert health_probes.connection_target(None) == ("", "")


async def test_probe_target_and_fetch_info_use_connection(monkeypatch):
    calls = []

    async def fake_check(base, key, **_):
        calls.append((base, key))
        return {"healthy": True, "version": "1.2.3", "db": "connected", "model_count": 7}

    monkeypatch.setattr(health_probes, "check_gateway", fake_check)
    conn = {"base_url": "http://lite:4000", "master_key": KEY}
    assert (await health_probes.probe_target(conn))["healthy"] is True
    info = await health_probes.fetch_info(conn)
    assert info == {"version": "1.2.3", "db_name": "connected", "item_count": 7}
    assert calls == [("http://lite:4000", KEY), ("http://lite:4000", KEY)]


async def test_fetch_info_unconfigured_is_empty():
    assert await health_probes.fetch_info({}) == {"version": None, "db_name": None, "item_count": None}


async def test_version_falls_back_to_openapi_and_is_cached():
    seen: list[httpx.Request] = []
    transport = _gateway(readiness_body={"status": "healthy", "db": "connected"}, record=seen)
    first = await health_probes.check_gateway("http://lite:4000", KEY, transport=transport)
    second = await health_probes.check_gateway("http://lite:4000", KEY, transport=transport)
    assert first["version"] == second["version"] == "1.103.3"
    assert sum(1 for r in seen if r.url.path == "/openapi.json") == 1  # 快取 10 分鐘
    assert all("authorization" not in r.headers for r in seen if r.url.path == "/openapi.json")


async def test_version_none_when_openapi_unavailable():
    transport = _gateway(readiness_body={"status": "healthy", "db": "connected"}, openapi_version=None)
    result = await health_probes.check_gateway("http://lite:4000", KEY, transport=transport)
    assert result["healthy"] is True and result["version"] is None


async def test_readiness_version_wins_without_openapi_call():
    seen: list[httpx.Request] = []
    await health_probes.check_gateway("http://lite:4000", KEY, transport=_gateway(record=seen))
    assert not any(r.url.path == "/openapi.json" for r in seen)
