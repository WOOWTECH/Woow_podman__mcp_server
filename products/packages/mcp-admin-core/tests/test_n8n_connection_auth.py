"""Regression tests: public settings must not prove Public API key authentication.

All saved settings and HTTP responses are synthetic. No config files or network
are read; the actual router function runs against httpx.MockTransport.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from n8n_mcp_admin.routers import config as config_mod


@pytest.fixture
def connection_probe(monkeypatch):
    requests = []
    saved = {"n8n_api_url": "https://qa.example.test", "n8n_api_key": "wrong-test-key"}
    mode = {"status": 401, "body": {"data": []}, "timeout": False, "ssrf_ok": True}

    class FakeStore:
        async def get(self, name, default=None):
            assert name == "connection"
            return saved

    async def check_url(url):
        return SimpleNamespace(ok=mode["ssrf_ok"], reason="private address")

    def respond(request):
        requests.append(request)
        if mode["timeout"]:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if request.url.path.endswith("/rest/settings"):
            # n8n's publicly reachable endpoint succeeds even for a wrong key.
            return httpx.Response(200, json={"data": {"versionCli": "2.35.3"}})
        if request.url.path.endswith("/api/v1/workflows"):
            assert dict(request.url.params) == {"limit": "1"}
            if request.headers.get("X-N8N-API-KEY") != "valid-test-key":
                return httpx.Response(mode["status"], json={"message": "synthetic denied"})
            return httpx.Response(200, json=mode["body"])
        return httpx.Response(404, json={"message": "unexpected path"})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        assert kwargs["follow_redirects"] is False
        return real_client(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(config_mod, "get_config_store", lambda: FakeStore())
    monkeypatch.setattr(config_mod, "check_url", check_url)
    monkeypatch.setattr(config_mod.httpx, "AsyncClient", fake_client)

    def run():
        return asyncio.run(config_mod.test_connection())

    return saved, mode, requests, run


@pytest.mark.parametrize("status", [401, 403])
def test_wrong_key_cannot_pass_public_settings(connection_probe, status):
    saved, mode, requests, run = connection_probe
    mode["status"] = status
    result = run()
    assert result.success is False, "Public /rest/settings cannot validate an API key"
    assert str(status) in result.message
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/workflows"


def test_missing_key_fails_without_http(connection_probe):
    saved, mode, requests, run = connection_probe
    saved["n8n_api_key"] = ""
    result = run()
    assert result.success is False
    assert "n8n_api_key" in result.message
    assert requests == []


@pytest.mark.parametrize("url", ["https://qa.example.test", "https://qa.example.test/", "https://qa.example.test/api/v1/"])
def test_valid_key_proves_minimal_authenticated_public_api_read(connection_probe, url):
    saved, mode, requests, run = connection_probe
    saved.update(n8n_api_url=url, n8n_api_key="valid-test-key")
    result = run()
    assert result.success is True
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://qa.example.test/api/v1/workflows?limit=1"
    assert request.headers["X-N8N-API-KEY"] == "valid-test-key"
    assert result.details["status_code"] == 200
    assert result.n8n_version is None  # Workflows response is not a version source.


def test_non_n8n_success_body_is_not_authentication_proof(connection_probe):
    saved, mode, requests, run = connection_probe
    saved["n8n_api_key"] = "valid-test-key"
    mode["body"] = {"data": {"versionCli": "2.35.3"}}
    assert run().success is False


def test_ssrf_block_still_prevents_any_http(connection_probe):
    saved, mode, requests, run = connection_probe
    mode["ssrf_ok"] = False
    result = run()
    assert result.success is False
    assert "SSRF" in result.message
    assert requests == []


def test_timeout_is_a_failure(connection_probe):
    saved, mode, requests, run = connection_probe
    mode["timeout"] = True
    assert run().success is False
