"""Settings API secret masking through the real n8n app assembly."""

from __future__ import annotations

from management_client import revision_headers

import json
import os

import pytest
from fastapi.testclient import TestClient

import mcp_admin_core.config.store as store_mod
from n8n_mcp_admin.main import app


@pytest.fixture
def settings_client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config = json.loads(json.dumps(store_mod._DEFAULT_CONFIG))
    config.update({
        "mcp_auth_token": "n8n-mcp-auth-token-test-value",
        "proxy": {"timeout": 4321, "bearer_token": "n8n-proxy-bearer-test-value"},
    })
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream)
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))
    monkeypatch.setattr(store_mod, "_instance", None)
    return TestClient(app), config_path


@pytest.mark.parametrize("bearer_token", ["n8n-proxy-bearer-test-value", "tiny"], ids=["long", "short"])
def test_n8n_settings_mask_credentials_and_preserve_masked_put(settings_client, bearer_token):
    client, config_path = settings_client
    config = json.loads(config_path.read_text())
    config["proxy"]["bearer_token"] = bearer_token
    config_path.write_text(json.dumps(config))
    login = client.post("/api/auth/login", json={"password": "admin"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    response = client.get("/api/settings", headers=headers)
    assert response.status_code == 200
    assert "n8n-mcp-auth-token-test-value" not in response.text
    assert bearer_token not in response.text
    assert "*" in response.json()["mcp_auth_token"]
    token_section = client.get("/api/settings/mcp_auth_token", headers=headers)
    assert "n8n-mcp-auth-token-test-value" not in token_section.text
    assert "*" in token_section.json()["value"]
    if bearer_token == "tiny":
        assert response.json()["proxy"]["bearer_token"] == "****"
    else:
        assert "*" in response.json()["proxy"]["bearer_token"]

    proxy = client.get("/api/settings/proxy", headers=headers)
    assert bearer_token not in proxy.text
    assert client.put("/api/settings/proxy", json=proxy.json(), headers=revision_headers(client, headers, read_path='/api/settings')).status_code == 200
    assert json.loads(config_path.read_text())["proxy"]["bearer_token"] == bearer_token
    assert client.put("/api/settings/proxy", json={"timeout": 4322, "bearer_token": ""}, headers=revision_headers(client, headers, read_path='/api/settings')).status_code == 200
    assert json.loads(config_path.read_text())["proxy"]["bearer_token"] == bearer_token

    client.cookies.clear()
    assert client.post("/api/settings/mcp_auth_token/reveal").status_code == 401
    assert client.post("/api/settings/proxy/bearer_token/reveal").status_code == 401
    assert client.post("/api/settings/mcp_auth_token/reveal", headers=headers).json()["value"] == "n8n-mcp-auth-token-test-value"
    assert client.post("/api/settings/proxy/bearer_token/reveal", headers=headers).json()["value"] == bearer_token

    for path in ("mcp_auth_token/reveal", "proxy/bearer_token/reveal"):
        revealed = client.post(f"/api/settings/{path}", headers=headers)
        assert revealed.headers.get("cache-control") == "no-store"
