"""Validate user-facing Quadlet settings and separation from the Podman socket."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


DEPLOY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("headscale_quadlet_settings", DEPLOY / "validate_env.py")
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)


def test_local_default_and_https_proxy_modes():
    settings.validate("127.0.0.1", "18081", "http://127.0.0.1:18081", "false", "")
    settings.validate("127.0.0.1", "18081", "https://mcp.example.com", "true", "10.42.3.4/32")


@pytest.mark.parametrize("bind,port,origin,cookie,cidrs", [
    ("0.0.0.0", "18081", "https://mcp.example.com", "true", ""),
    ("127.0.0.1", "0", "http://127.0.0.1:18081", "false", ""),
    ("127.0.0.1", "18081", "https://mcp.example.com/path", "true", ""),
    ("127.0.0.1", "18081", "https://mcp.example.com", "false", ""),
    ("127.0.0.1", "18081", "http://mcp.example.com", "false", ""),
    ("127.0.0.1", "18081", "http://127.0.0.1:18081", "true", ""),
    ("127.0.0.1", "18081", "http://127.0.0.1:18081", "false", "0.0.0.0/0"),
    ("127.0.0.1", "18081", "http://127.0.0.1:18081", "false", "bad-cidr"),
])
def test_unsafe_or_unusable_settings_rejected(bind, port, origin, cookie, cidrs):
    with pytest.raises(ValueError):
        settings.validate(bind, port, origin, cookie, cidrs)


def test_unit_has_independent_state_and_no_podman_socket():
    unit = (DEPLOY / "quadlet/headscale-mcp-admin.container").read_text()
    assert "ContainerName=headscale-mcp-admin" in unit
    assert "Volume=headscale-mcp.volume:/data:U" in unit
    assert "PublishPort=@@MCP_ADMIN_BIND@@:@@MCP_ADMIN_PORT@@:8080" in unit
    assert "podman.sock" not in unit and "%t/podman" not in unit
    assert "Secret=headscale-mcp-admin-password,type=env,target=ADMIN_PASSWORD" in unit
    assert "Secret=headscale-mcp-admin-jwt-secret,type=env,target=JWT_SECRET" in unit


def test_image_tag_matches_pinned_product_version():
    version = (DEPLOY / "VERSION").read_text().strip()
    unit = (DEPLOY / "quadlet/headscale-mcp-admin.container").read_text()
    assert f"Image=localhost/woow-headscale-mcp-admin:{version}" in unit
