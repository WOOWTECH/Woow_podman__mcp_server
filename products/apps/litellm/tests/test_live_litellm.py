"""Opt-in live probe against a real, reachable LiteLLM gateway.

Excluded from the default run (``addopts = -m "not live"``). Enable with::

    LITELLM_MCP_BASE_URL=http://litellm.litellm.svc.cluster.local:4000 \
    LITELLM_MCP_MASTER_KEY=sk-... \
    pytest -m live

Inside the sandbox, external hostnames are blocked, so this is normally run from
a pod (via k3s exec) against the in-cluster Service URL. It replaces the
reference repo's ``test_live_emqx``.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.live


def _cfg() -> tuple[str, str]:
    base = os.environ.get("LITELLM_MCP_BASE_URL", "").rstrip("/")
    key = os.environ.get("LITELLM_MCP_MASTER_KEY", "")
    if not base:
        pytest.skip("LITELLM_MCP_BASE_URL not set; skipping live probe")
    return base, key


async def test_health_readiness_is_reachable() -> None:
    base, key = _cfg()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        resp = await client.get(f"{base}/health/readiness")
    assert resp.status_code < 500
    if resp.status_code == 200:
        body = resp.json()
        assert isinstance(body, dict)
        # LiteLLM reports its version and DB status here.
        assert body.get("status") is not None or body.get("db") is not None


async def test_models_endpoint_returns_openai_envelope() -> None:
    base, key = _cfg()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        resp = await client.get(f"{base}/v1/models")
    if resp.status_code == 401:
        pytest.skip("master key not accepted; set LITELLM_MCP_MASTER_KEY")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body and isinstance(body["data"], list)
