"""OAuth 探測路徑要回 404 JSON，不能回 SPA index.html（商用驗收 AI-HMCP-003-3 第 3 項）。

Hermes MCP 249 實測：MCP URL 的 token 打錯時 /private_<token>/mcp 回 403 JSON，但 Claude Code 接著探測的
/.well-known/oauth-protected-resource、/.well-known/oauth-authorization-server、/register 落到 SPA fallback
回 index.html＋200，Claude Code 因此判成 needs-auth（暗示要走 OAuth），而不是「連線失敗」。

管理後台沒有 OAuth：這些路徑一律 404 JSON（GET），動態註冊的 POST /register 也只能拿到 JSON 錯誤。
實作在 #60（SPA fallback 白名單）；本檔把 MCP 用戶端實際會探測的路徑釘成回歸測試。
"""

from __future__ import annotations

import json

import pytest

INDEX = "<html><body>admin-spa</body></html>"
TOKEN = "qa-token-1234567890"


@pytest.fixture
def client(tmp_path, tmp_config_path, monkeypatch):
    import mcp_admin_core.config.store as store_mod
    from fastapi.testclient import TestClient

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX)

    tmp_config_path.write_text(json.dumps({**store_mod._DEFAULT_CONFIG, "mcp_auth_token": TOKEN}))
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    store_mod.get_config_store()

    from mcp_admin_core import create_app

    return TestClient(create_app(title="OAuth Probe Test", static_dir=static))


# RFC 9728／RFC 8414／OIDC 的探測路徑，含 MCP 規格的「路徑後綴」變體，以及預設的動態註冊端點
DISCOVERY_PATHS = [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/private_wrong-token/mcp",
    f"/.well-known/oauth-protected-resource/private_{TOKEN}/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/private_wrong-token/mcp",
    "/.well-known/openid-configuration",
    "/register",
    "/authorize",
    "/token",
]


@pytest.mark.parametrize("path", DISCOVERY_PATHS)
def test_oauth_discovery_get_is_404_json(client, path):
    r = client.get(path)
    assert r.status_code == 404, f"{path} 應為 404，實得 {r.status_code}"
    assert r.headers["content-type"].startswith("application/json")
    assert "admin-spa" not in r.text
    assert r.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", ["/register", "/token"])
def test_dynamic_client_registration_post_is_json_error(client, path):
    """POST 沒有對應路由（405），重點是拿到 JSON 錯誤、不是 200／HTML。"""
    r = client.post(path, json={"client_name": "claude-code", "redirect_uris": ["http://localhost/cb"]})
    assert r.status_code in (404, 405)
    assert r.headers["content-type"].startswith("application/json")
    assert "admin-spa" not in r.text


def test_wrong_token_mcp_path_is_403_json(client):
    r = client.post("/private_wrong-token/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/json")
