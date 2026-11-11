"""drop_blank_secrets 行為測試 —— 空 secret 移除、非 secret/非空原樣、整合淺合併保留。"""

from __future__ import annotations

from mcp_admin_core.util.secrets import drop_blank_secrets


def test_blank_secret_is_dropped():
    out = drop_blank_secrets({"gateway_api_key": "", "gateway_url": "http://x"}, ["gateway_api_key"])
    assert "gateway_api_key" not in out
    assert out["gateway_url"] == "http://x"


def test_whitespace_only_secret_is_dropped():
    out = drop_blank_secrets({"k": "   "}, ["k"])
    assert "k" not in out


def test_nonblank_secret_passes_through():
    out = drop_blank_secrets({"k": "real-key"}, ["k"])
    assert out["k"] == "real-key"


def test_non_secret_blank_is_kept():
    out = drop_blank_secrets({"dashboard_username": ""}, ["gateway_api_key"])
    assert out["dashboard_username"] == ""


def test_original_updates_not_mutated():
    updates = {"k": ""}
    drop_blank_secrets(updates, ["k"])
    assert updates == {"k": ""}


def test_multiple_secret_fields():
    out = drop_blank_secrets(
        {"gateway_api_key": "", "dashboard_password": "pw", "gateway_url": "u"},
        ["gateway_api_key", "dashboard_password"],
    )
    assert "gateway_api_key" not in out
    assert out["dashboard_password"] == "pw"
    assert out["gateway_url"] == "u"


# 整合 smoke：證明守門正確接進 ConfigStore.patch 的淺合併（helper 單測與 app
# update_connection 之間的接線缺口）。fresh_store 為 core 既有 conftest fixture；
# async 測試為 pytest-asyncio auto mode（bare async def）。
async def test_guard_preserves_secret_through_configstore_patch(fresh_store):
    await fresh_store.patch("connection", drop_blank_secrets(
        {"gateway_url": "http://gw", "gateway_api_key": "real-key"},
        ["gateway_api_key"],
    ))
    # 模擬「只改 URL、沒重打 key」：空 key 被守門移除 → patch 合併保留既有 key
    merged = await fresh_store.patch("connection", drop_blank_secrets(
        {"gateway_url": "http://gw2", "gateway_api_key": ""},
        ["gateway_api_key"],
    ))
    assert merged["gateway_api_key"] == "real-key"  # 保留
    assert merged["gateway_url"] == "http://gw2"     # 更新
