"""vk health probes 契約測試：/api/health 端點、URL 組合、錯誤降級、fetch_info 形狀守衛。"""
from __future__ import annotations

import httpx
import pytest


class _FakeAsyncClient:
    """假 httpx.AsyncClient：記錄 GET URL，回預設回應或丟預設例外。"""
    calls: list[str] = []
    response: httpx.Response | None = None
    raise_exc: Exception | None = None

    def __init__(self, *args, **kwargs): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def get(self, url, **kwargs):
        _FakeAsyncClient.calls.append(str(url))
        if _FakeAsyncClient.raise_exc is not None:
            raise _FakeAsyncClient.raise_exc
        return _FakeAsyncClient.response


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    from vk_mcp_admin import health_probes as hp
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = None
    _FakeAsyncClient.raise_exc = None
    monkeypatch.setattr(hp.httpx, "AsyncClient", _FakeAsyncClient)
    yield


def _resp(status=200, json_body=None, url="http://vk-host:3000/api/health"):
    req = httpx.Request("GET", url)
    return httpx.Response(status, request=req, json=json_body if json_body is not None else {})


# ---------------------------------------------------------------------------
# probe_target：健康探測（healthy==200 正／反面、錯誤降級各分支）
# ---------------------------------------------------------------------------


async def test_probe_target_hits_vk_health_endpoint():
    from vk_mcp_admin.health_probes import VK_HEALTH_PATH, probe_target
    _FakeAsyncClient.response = _resp(200)
    out = await probe_target({"vibe_backend_url": "http://vk-host:3000/"})   # 尾斜線需被 rstrip
    # 斷言引用單一常數：端點 Gate 2 已定案 /api/health，未來再變只改 health_probes.VK_HEALTH_PATH 一行
    assert _FakeAsyncClient.calls == [f"http://vk-host:3000{VK_HEALTH_PATH}"]
    assert out["healthy"] is True and out["status_code"] == 200


async def test_probe_target_unconfigured_is_unhealthy_without_http_call():
    from vk_mcp_admin.health_probes import probe_target
    out = await probe_target({})
    assert out["healthy"] is False and _FakeAsyncClient.calls == []


async def test_probe_target_non_200_is_unhealthy_with_status_code():
    # 鎖 healthy == (status_code == 200) 的反面：503 時 healthy 必為 False 且回傳原始狀態碼，
    # 防有人把判定放寬成「有回應即健康」而假 GREEN。
    from vk_mcp_admin.health_probes import probe_target
    _FakeAsyncClient.response = _resp(503)
    out = await probe_target({"vibe_backend_url": "http://vk-host:3000"})
    assert out["healthy"] is False and out["status_code"] == 503


async def test_probe_target_connect_error_degrades():
    # 連線層錯誤 → 降級並回固定可讀訊息（鎖 ConnectError 分支的 "Connection refused"，
    # 非只驗有 error 鍵，才能與逾時分支分辨）。
    from vk_mcp_admin.health_probes import probe_target
    _FakeAsyncClient.raise_exc = httpx.ConnectError("refused")
    out = await probe_target({"vibe_backend_url": "http://vk-host:3000"})
    assert out["healthy"] is False and out["error"] == "Connection refused"


async def test_probe_target_timeout_degrades():
    # 逾時 → 走 TimeoutException 分支，回 "Timed out"（與 ConnectError 分支需可辨別）。
    from vk_mcp_admin.health_probes import probe_target
    _FakeAsyncClient.raise_exc = httpx.TimeoutException("slow")
    out = await probe_target({"vibe_backend_url": "http://vk-host:3000"})
    assert out["healthy"] is False and out["error"] == "Timed out"


# ---------------------------------------------------------------------------
# fetch_info：best-effort 版本抓取（body 形狀守衛、永不 raise）
# ---------------------------------------------------------------------------


async def test_fetch_info_shape_and_version_best_effort():
    from vk_mcp_admin.health_probes import fetch_info
    _FakeAsyncClient.response = _resp(200, json_body={"version": "0.1.44"})
    info = await fetch_info({"vibe_backend_url": "http://vk-host:3000"})
    assert info == {"version": "0.1.44", "db_name": None, "item_count": None}


async def test_fetch_info_non_dict_body_returns_none_fields():
    # 守衛 isinstance(body, dict)：200 但 body 為 JSON list（非 dict）→ version 不得誤填，
    # 三欄一律 None（鎖 plan:311「body 為 JSON dict 才填」規格）。
    from vk_mcp_admin.health_probes import fetch_info
    _FakeAsyncClient.response = _resp(200, json_body=["not", "a", "dict"])
    info = await fetch_info({"vibe_backend_url": "http://vk-host:3000"})
    assert info == {"version": None, "db_name": None, "item_count": None}


async def test_fetch_info_non_str_version_returns_none_fields():
    # 守衛 isinstance(version, str)：200 且 body 為 dict 但 version 為數字 → 不得填入，
    # version 保持 None（鎖 plan:311「version 為 str 才填」規格）。
    from vk_mcp_admin.health_probes import fetch_info
    _FakeAsyncClient.response = _resp(200, json_body={"version": 123})
    info = await fetch_info({"vibe_backend_url": "http://vk-host:3000"})
    assert info == {"version": None, "db_name": None, "item_count": None}


async def test_fetch_info_invalid_json_body_degrades_without_raise():
    # 200 但 body 非合法 JSON（resp.json() 會 raise）→ best-effort 降級為三欄 None、
    # 絕不讓例外冒泡（dashboard 不因抓版本失敗而報錯）。
    from vk_mcp_admin.health_probes import fetch_info
    req = httpx.Request("GET", "http://vk-host:3000/api/health")
    _FakeAsyncClient.response = httpx.Response(200, request=req, content=b"not json")
    info = await fetch_info({"vibe_backend_url": "http://vk-host:3000"})
    assert info == {"version": None, "db_name": None, "item_count": None}


async def test_fetch_info_error_path_returns_none_fields():
    # 例外路徑（此處以 TimeoutException 代表連線層／逾時）→ 降級三欄 None、永不 raise。
    from vk_mcp_admin.health_probes import fetch_info
    _FakeAsyncClient.raise_exc = httpx.TimeoutException("slow")
    info = await fetch_info({"vibe_backend_url": "http://vk-host:3000"})
    assert info == {"version": None, "db_name": None, "item_count": None}
