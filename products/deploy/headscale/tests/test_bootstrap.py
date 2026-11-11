"""First boot must never replace persisted management credentials."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest


BOOT = Path(__file__).resolve().parents[1] / "bootstrap.py"
spec = importlib.util.spec_from_file_location("headscale_quadlet_bootstrap", BOOT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def test_first_boot_seeds_private_config(tmp_path):
    path = tmp_path / "config.json"
    assert bootstrap.seed_config(path, "a-long-first-boot-password") is True
    assert json.loads(path.read_text()) == {"admin_password": "a-long-first-boot-password"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".config-seed-*"))


def test_existing_config_is_preserved_without_seed_secret(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"admin_password":"existing"}')
    before = path.read_bytes()
    assert bootstrap.seed_config(path, "") is False
    assert path.read_bytes() == before


def test_existing_symlink_is_never_followed_or_replaced(tmp_path):
    target = tmp_path / "target"
    target.write_text("do not touch")
    path = tmp_path / "config.json"
    path.symlink_to(target)
    assert bootstrap.seed_config(path, "a-long-first-boot-password") is False
    assert target.read_text() == "do not touch"


@pytest.mark.parametrize("secret", ["", "admin", "too-short", "   short   "])
def test_missing_or_weak_new_password_rejected(tmp_path, secret):
    with pytest.raises(ValueError, match="admin password"):
        bootstrap.seed_config(tmp_path / "config.json", secret)


def test_main_removes_seed_secret_before_app_import(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(path))
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-first-boot-password")
    called = []
    monkeypatch.setattr(bootstrap.uvicorn, "run", lambda app, **kwargs:
                        called.append((app, kwargs, os.environ.get("ADMIN_PASSWORD"))))
    bootstrap.main()
    assert called == [("headscale_mcp_admin.main:app", {"host": "0.0.0.0", "port": 8080}, None)]
