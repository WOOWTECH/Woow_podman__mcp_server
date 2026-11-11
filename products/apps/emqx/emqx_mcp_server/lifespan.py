"""One pooled httpx client for the whole server lifetime."""

from __future__ import annotations

from .http_policy import BrokerClient
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from .settings import Settings


def make_client() -> BrokerClient:
    cfg = Settings()
    return BrokerClient(cfg.base_url, cfg.api_key, cfg.api_secret, cfg.request_timeout)


@lifespan
async def emqx_lifespan(server: FastMCP):
    client = make_client()
    try:
        yield {"emqx": client}
    finally:
        await client.aclose()
