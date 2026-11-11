"""health_probes：以唯讀 get_file_tree("", 1) 判定 healthy；/status.php 只補版本；結果不含 app password。"""
from __future__ import annotations

import httpx
import pytest

from nextcloud_mcp_admin import health_probes

from .conftest import CONN, AuthCheckingNextcloud
from .fake_nextcloud import BASE_URL, LOGIN, PASSWORD


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    monkeypatch.setattr(health_probes, "_version_cache", {})
    monkeypatch.setattr(health_probes, "_last_probe", None)


async def test_healthy_with_valid_credentials(authed, fake):
    result = await health_probes.check_nextcloud(BASE_URL, LOGIN, PASSWORD, transport=authed.transport)
    assert result["healthy"] is True and result["auth_ok"] is True and result["error"] is None
    assert result["item_count"] == 3 and result["truncated"] is False  # Docs、Empty、photo.png
    assert result["version"] == "35.0.1"
    # 只做唯讀請求：OCS user id ＋ 家目錄 PROPFIND depth 1
    methods = [(r.method, r.headers.get("Depth")) for r in fake.requests]
    assert methods == [("GET", None), ("PROPFIND", "1")]
    assert PASSWORD not in repr(result)


async def test_wrong_password(fake):
    nc = AuthCheckingNextcloud(fake, password="Another-Password-123")
    result = await health_probes.check_nextcloud(BASE_URL, LOGIN, PASSWORD, transport=nc.transport)
    assert result["healthy"] is False and result["auth_ok"] is False
    assert result["error"] == "Nextcloud rejected the username or app password."
    assert result["version"] == "35.0.1"  # 版本仍可補（免認證）
    assert PASSWORD not in repr(result)


async def test_new_password_after_wrong_one_is_checked_again(fake):
    """管理介面是長駐 process：錯密碼鎖存後，存了正確的 app password 要能立刻測試成功（鎖存依帳密分鍵）。"""
    wrong = AuthCheckingNextcloud(fake, password="Another-Password-123")
    first = await health_probes.check_nextcloud(BASE_URL, LOGIN, PASSWORD, transport=wrong.transport)
    assert first["auth_ok"] is False and first["error"] == "Nextcloud rejected the username or app password."
    again = await health_probes.check_nextcloud(BASE_URL, LOGIN, PASSWORD, transport=wrong.transport)
    assert again["auth_ok"] is False and again["error"] == "Nextcloud rejected the username or app password."
    right = AuthCheckingNextcloud(fake, password="Another-Password-123")
    fixed = await health_probes.check_nextcloud(BASE_URL, LOGIN, "Another-Password-123", transport=right.transport)
    assert fixed["healthy"] is True and fixed["auth_ok"] is True


async def test_status_php_failure_does_not_decide(fake):
    nc = AuthCheckingNextcloud(fake, version=None)
    result = await health_probes.check_nextcloud(BASE_URL, LOGIN, PASSWORD, transport=nc.transport)
    assert result["healthy"] is True and result["version"] is None


async def test_network_error():
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    result = await health_probes.check_nextcloud(BASE_URL, LOGIN, PASSWORD, transport=httpx.MockTransport(boom))
    assert result["healthy"] is False
    assert result["error"].startswith("Could not reach Nextcloud")


async def test_redirect_is_reported():
    def redirect(request):
        if request.url.path.endswith("/status.php"):
            return httpx.Response(404)
        return httpx.Response(301, headers={"Location": "https://other.example.com/nc/ocs/v2.php/cloud/user"})

    result = await health_probes.check_nextcloud(BASE_URL, LOGIN, PASSWORD,
                                                 transport=httpx.MockTransport(redirect))
    assert result["healthy"] is False and "redirect" in result["error"]


@pytest.mark.parametrize(
    ("base", "user", "password", "fragment"),
    [("", LOGIN, PASSWORD, "Not configured"), (BASE_URL, "", PASSWORD, "帳號"),
     (BASE_URL, LOGIN, "", "app password"), ("ftp://x", LOGIN, PASSWORD, "設定不正確")],
)
async def test_incomplete_settings(base, user, password, fragment):
    result = await health_probes.check_nextcloud(base, user, password)
    assert result["healthy"] is False and fragment in result["error"]
    if password:
        assert password not in result["error"]


async def test_probe_target_and_fetch_info_share_one_probe(monkeypatch):
    calls = []

    async def fake_check(base, user, password, **_):
        calls.append(base)
        return {"healthy": True, "url": base, "version": "35.0.1", "item_count": 7}

    monkeypatch.setattr(health_probes, "check_nextcloud", fake_check)
    target = await health_probes.probe_target(dict(CONN))
    info = await health_probes.fetch_info(dict(CONN))
    assert target["healthy"] is True
    assert info == {"version": "35.0.1", "db_name": LOGIN, "item_count": 7}
    assert calls == [BASE_URL]  # fetch_info 重用剛才的探測結果


async def test_fetch_info_without_url():
    assert await health_probes.fetch_info({}) == {"version": None, "db_name": None, "item_count": None}


def test_connection_target_strips():
    assert health_probes.connection_target({"base_url": f" {BASE_URL}/ ", "username": f" {LOGIN} ",
                                            "app_password": PASSWORD}) == (BASE_URL, LOGIN, PASSWORD)
