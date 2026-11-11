"""Deployment contracts that must hold for every Quadlet MCP product."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SHARED = Path(__file__).resolve().parents[1]
PRODUCTS = SHARED.parents[1]


def module(name: str):
    spec = importlib.util.spec_from_file_location(name, SHARED / f"{name}.py")
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_catalog_covers_active_paas_apps_and_uses_separate_ports():
    catalog = module("catalog").load()
    names = set(catalog["products"])
    assert names == {
        "headscale", "emqx", "hermes", "n8n", "odoo", "litellm",
        "nextcloud", "opendesign", "k8s", "vk",
    }
    assert len({item["default_port"] for item in catalog["products"].values()}) == len(names)
    assert "odoo-manage" not in names


def test_opendesign_image_tag_changes_without_other_product_tag_drift():
    catalog = module("catalog").load()
    old_tag = "paas-c077be9-podman.1"
    new_tag = "paas-f0146c8-podman.1"
    assert catalog["image_version"] == old_tag
    assert {
        name: item["image_version"] for name, item in catalog["products"].items()
        if "image_version" in item
    } == {"opendesign": new_tag}
    for name in catalog["products"]:
        raw = subprocess.check_output([sys.executable, str(SHARED / "catalog.py"), "get", name])
        fields = raw.rstrip(b"\0").decode().split("\0")
        assert len(fields) == 5
        assert fields[3] == (new_tag if name == "opendesign" else old_tag)


def test_first_boot_keeps_existing_config_and_n8n_child_bearer(tmp_path):
    seed = module("bootstrap").seed_config
    path = tmp_path / "state" / "config.json"
    assert seed(path, "admin-password-at-least-12", "n8n_mcp_admin.main:app")
    stored = json.loads(path.read_text())
    assert stored["mcp_server"]["env"]["AUTH_TOKEN"] == stored["proxy"]["bearer_token"]
    assert stored["mcp_server"]["port"] == 3000
    assert len(stored["proxy"]["bearer_token"]) >= 32
    assert path.stat().st_mode & 0o777 == 0o600
    assert not seed(path, "different-password-at-least-12", "n8n_mcp_admin.main:app")
    assert json.loads(path.read_text()) == stored


def test_first_boot_rejects_weak_password_and_generic_has_no_upstream_secret(tmp_path):
    seed = module("bootstrap").seed_config
    path = tmp_path / "config.json"
    with pytest.raises(ValueError):
        seed(path, "admin", "emqx_mcp_admin.main:app")
    assert not path.exists()
    assert seed(path, "admin-password-at-least-12", "emqx_mcp_admin.main:app")
    assert json.loads(path.read_text()) == {"admin_password": "admin-password-at-least-12"}


def test_restore_revokes_old_admin_session_generation(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"admin_password": "strong-admin-password", "admin_session_generation": "old"}))
    os.chmod(path, 0o600)
    module("rotate-restored-auth").rotate(path)
    restored = json.loads(path.read_text())
    assert restored["admin_session_generation"] != "old"
    assert restored["admin_password"] == "strong-admin-password"
    assert path.stat().st_mode & 0o777 == 0o600


def test_quadlet_templates_keep_secret_and_data_boundary():
    container = (SHARED / "quadlet/product.container").read_text()
    assert "Secret=@@APP@@-jwt-secret,type=env,target=JWT_SECRET" in container
    assert "Secret=@@APP@@-password,type=env,target=ADMIN_PASSWORD" in container
    assert "PublishPort=@@MCP_ADMIN_BIND@@:@@MCP_ADMIN_PORT@@:8080" in container
    assert "ReadOnly=true" in container
    assert "NoNewPrivileges=true" in container
    assert "podman.sock" not in container
    assert "Volume=@@APP@@.volume:/data:U" in container
