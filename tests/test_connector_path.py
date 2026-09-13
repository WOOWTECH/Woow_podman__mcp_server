"""Regression: the smoke test must POST to the path the stack actually serves.

``tests/smoke.sh`` built its connector URL as ``/private_<token>/mcp/`` -- with a
trailing slash.  The proxy forwards everything after ``/private_<token>/``
verbatim (``mcp_admin_core/proxy.py``), and the supervised MCP child is mounted
at ``--path /mcp`` (``podman_mcp_admin/bootstrap.py``), so the slashed form is an
alias the child answers with a 307.  ``curl`` in the smoke test has no ``-L``, so
the check read 307, compared it against 200 and failed on a stack that was
working perfectly.

Nothing caught this in review because nothing executed the MCP check outside a
live host.  The test below does execute it: it drives the real proxy route with
the path taken out of ``tests/smoke.sh``, against an upstream that behaves the
way the MCP child does.

Deliberately no ``follow_redirects``: that is exactly what ``curl`` without
``-L`` does, and it is the assertion we want.  Adding ``-L`` to the smoke test
would have made it green too, but it would also have made it blind -- the proxy
rewrites upstream ``Location`` headers to keep a redirect inside the
``/private_<token>`` namespace (proxy.py), and a check that follows redirects
blindly can no longer tell a correct rewrite from a leak to the client's own
loopback.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from mcp_admin_core import proxy
from podman_mcp_admin.bootstrap import _child_argv

REPO = Path(__file__).resolve().parent.parent
TOKEN = "smoke-regression-token"  # noqa: S105 - a test fixture, not a credential
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}


def child_mount_path() -> str:
    """The path the supervised MCP child is actually mounted on."""
    argv = _child_argv(8000)
    return argv[argv.index("--path") + 1]


def smoke_connector_suffix() -> str:
    """What tests/smoke.sh appends after ``/private_<token>/``."""
    text = (REPO / "tests" / "smoke.sh").read_text()
    found = re.findall(r'/private_%s/([^"\s]*)"', text)
    assert found, "tests/smoke.sh no longer builds a /private_%s/... connector URL"
    assert len(set(found)) == 1, f"tests/smoke.sh builds more than one connector URL: {found}"
    return found[0]


def _upstream(request: httpx.Request) -> httpx.Response:
    """Stand in for the MCP child: it serves its mount path and 307s the alias."""
    if request.url.path == child_mount_path():
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    if request.url.path == child_mount_path() + "/":
        # FastMCP builds this Location from the upstream Host header.
        return httpx.Response(307, headers={"location": "http://localhost" + child_mount_path()})
    return httpx.Response(404)


class _Store:
    async def get(self, key: str, default=None):  # noqa: ANN001, ANN201
        return {
            "mcp_auth_token": TOKEN,
            "mcp_server": {"port": 8000},
            "proxy": {},
        }.get(key, default)


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The proxy router over ASGI, on this test's own event loop.

    Deliberately not ``fastapi.testclient.TestClient``: that drives the app from
    a portal thread with an event loop of its own, which leaves the asyncio child
    watcher in a state that breaks the supervision tests' real subprocesses when
    they run afterwards in the same session.
    """
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
    monkeypatch.setattr(proxy, "get_config_store", _Store)
    monkeypatch.setattr(proxy, "_get_client", lambda _timeout: upstream)
    app = FastAPI()
    app.include_router(proxy.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as c:
        yield c
    await upstream.aclose()


def test_smoke_connector_url_is_the_path_the_child_is_mounted_on() -> None:
    """The URL in tests/smoke.sh must be the child's mount path, slash for slash."""
    assert "/" + smoke_connector_suffix() == child_mount_path(), (
        f"tests/smoke.sh POSTs to /private_<token>/{smoke_connector_suffix()} but the MCP child "
        f"is mounted at {child_mount_path()}; the difference is answered with a redirect that "
        f"curl without -L reports as 307"
    )


async def test_smoke_connector_url_answers_200_without_following_redirects(client) -> None:  # noqa: ANN001
    """The failing path itself: POST what smoke.sh posts, read what curl reads."""
    resp = await client.post(f"/private_{TOKEN}/{smoke_connector_suffix()}", json=INIT)
    assert resp.status_code == 200, (
        f"the smoke test's connector URL answered {resp.status_code}; curl without -L compares "
        f"that against 200 and the check fails on a healthy stack"
    )


async def test_a_wrong_token_is_still_403(client) -> None:  # noqa: ANN001
    """The sibling assertion the fix must not weaken."""
    resp = await client.post(f"/private_wrong-{TOKEN}/{smoke_connector_suffix()}", json=INIT)
    assert resp.status_code == 403


async def test_the_slashed_alias_is_what_redirects(client) -> None:  # noqa: ANN001
    """Pin the mechanism, so a future routing change shows up here and not on a host."""
    resp = await client.post(f"/private_{TOKEN}{child_mount_path()}/", json=INIT)
    assert resp.status_code == 307
