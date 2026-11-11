"""Synthetic v4 pair error shapes through the actual PaaS MCP tool seam."""

from __future__ import annotations

import json
from xml.sax.saxutils import escape

import httpx
import pytest
from fastmcp import Client

from .conftest import AuthCheckingNextcloud
from .fake_nextcloud import PASSWORD
from .test_mcp_surface import _server_for_policy


DIAGNOSTIC = "FAKE quota exhausted on isolated backend"
HOME_FOLDER_DIAGNOSTIC = "Quota of the home folder is exhausted on node-7"
TOKEN = "FAKE-opaque-mcp-token-abcdef0123456789"
INTERNAL_HOST = "nextcloud-db.internal.svc.cluster.local"
CASES = [(status, shape) for status in (403, 422, 500, 503)
         for shape in ("sabre", "ocs_json", "text")]


def _answer(status: int, shape: str, message: str) -> httpx.Response:
    if shape == "sabre":
        body = ('<d:error xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">'
                f'<s:message>{escape(message)}</s:message></d:error>').encode()
        return httpx.Response(status, content=body, headers={"content-type": "application/xml"})
    if shape == "ocs_json":
        body = json.dumps({"ocs": {"meta": {"message": message}}}).encode()
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})
    return httpx.Response(status, content=message.encode(),
                          headers={"content-type": "text/plain; charset=utf-8"})


async def _call(monkeypatch, tmp_path, fake, response: httpx.Response) -> str:
    fake.override = lambda request: response
    authed = AuthCheckingNextcloud(fake)
    server = _server_for_policy(monkeypatch, tmp_path, [], authed)
    async with Client(server) as client:
        result = await client.call_tool_mcp("get_file_tree", {"path": "case.txt"})
    assert result.is_error is True
    assert authed.auth_headers and all(header == authed.expected for header in authed.auth_headers)
    assert all(request.method in ("GET", "PROPFIND") for request in fake.requests)
    return "\n".join(item.text for item in result.content if item.type == "text")


@pytest.mark.parametrize(("status", "shape"), CASES)
@pytest.mark.parametrize("diagnostic", (DIAGNOSTIC, HOME_FOLDER_DIAGNOSTIC))
async def test_quota_error_shapes_return_safe_diagnostic(
    monkeypatch, tmp_path, fake, status, shape, diagnostic,
):
    message = f"{diagnostic}; app_password={PASSWORD}; token={TOKEN}; host={INTERNAL_HOST}"
    output = await _call(monkeypatch, tmp_path, fake, _answer(status, shape, message))
    assert "storage quota exhausted" in output
    assert all(value not in output for value in (PASSWORD, TOKEN, INTERNAL_HOST, diagnostic))


@pytest.mark.parametrize("shape", ("sabre", "ocs_json", "text"))
async def test_unknown_backend_text_is_not_reflected(monkeypatch, tmp_path, fake, shape):
    message = f"unexpected error token={TOKEN} password={PASSWORD} at http://{INTERNAL_HOST}/admin"
    output = await _call(monkeypatch, tmp_path, fake, _answer(422, shape, message))
    assert "Nextcloud refused" in output
    assert all(value not in output for value in (TOKEN, PASSWORD, INTERNAL_HOST, "unexpected error"))


async def test_redirect_does_not_reveal_internal_location(monkeypatch, tmp_path, fake):
    response = httpx.Response(302, headers={
        "location": f"http://{INTERNAL_HOST}/internal/redirect?token={TOKEN}"})
    output = await _call(monkeypatch, tmp_path, fake, response)
    assert "redirect" in output.lower()
    assert INTERNAL_HOST not in output and TOKEN not in output
