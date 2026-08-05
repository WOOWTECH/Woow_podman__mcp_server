"""Regressions for the two P0s fixed while forking mcp_admin_core from litellm.

Both were latent in the reference and both fail *open*: a corrupt config reset
the admin password to a known value and blanked the connector token, and the
seeded default password was the literal string "admin".
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mcp_admin_core.config.store import ConfigCorruptError, ConfigStore


@pytest.fixture()
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


# -- P0 #2: no hardcoded admin password -------------------------------------


def test_seeds_password_from_env(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "from-the-environment")
    ConfigStore(cfg_path)
    assert json.loads(cfg_path.read_text())["admin_password"] == "from-the-environment"


def test_generates_password_when_env_absent(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    ConfigStore(cfg_path)
    password = json.loads(cfg_path.read_text())["admin_password"]
    assert password != "admin", "the litellm default password must not survive the fork"
    assert len(password) >= 16


def test_generated_passwords_differ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    ConfigStore(first)
    ConfigStore(second)
    assert (
        json.loads(first.read_text())["admin_password"]
        != json.loads(second.read_text())["admin_password"]
    )


def test_blank_env_password_is_treated_as_unset(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``ADMIN_PASSWORD=`` in a .env file must not seed an empty password.

    An empty stored password would make the login check compare against "" —
    which is what an operator who submits the form with an empty field sends.
    """
    monkeypatch.setenv("ADMIN_PASSWORD", "   ")
    ConfigStore(cfg_path)
    assert json.loads(cfg_path.read_text())["admin_password"].strip() != ""


def test_config_is_chmod_600(cfg_path: Path) -> None:
    ConfigStore(cfg_path)
    assert oct(cfg_path.stat().st_mode)[-3:] == "600"


# -- P0 #1: atomic write, fatal on corruption --------------------------------


@pytest.mark.asyncio
async def test_write_is_atomic_and_leaves_no_temp_file(cfg_path: Path) -> None:
    store = ConfigStore(cfg_path)
    await store.put("mcp_auth_token", "tok-abc")
    assert json.loads(cfg_path.read_text())["mcp_auth_token"] == "tok-abc"
    assert not cfg_path.with_name(cfg_path.name + ".tmp").exists()
    assert oct(cfg_path.stat().st_mode)[-3:] == "600"


@pytest.mark.asyncio
async def test_truncated_config_raises_instead_of_resetting(cfg_path: Path) -> None:
    """The failure mode this replaces: silent fallback to _DEFAULT_CONFIG.

    That reset ``admin_password`` to a known value and blanked
    ``mcp_auth_token`` — every connector 403s AND the console opens up.
    """
    cfg_path.write_text('{"admin_password": "kept", "mcp_auth_token": "tok"')  # truncated
    with pytest.raises(ConfigCorruptError):
        await ConfigStore(cfg_path).load()


@pytest.mark.asyncio
async def test_non_object_config_raises(cfg_path: Path) -> None:
    cfg_path.write_text("[1, 2, 3]")
    with pytest.raises(ConfigCorruptError):
        await ConfigStore(cfg_path).load()


@pytest.mark.asyncio
async def test_missing_file_is_still_recoverable(cfg_path: Path) -> None:
    """A *missing* file is not corruption — it re-seeds, as before."""
    store = ConfigStore(cfg_path)
    cfg_path.unlink()
    loaded = await store.reload()
    assert isinstance(loaded, dict)
    assert "tools" in loaded


@pytest.mark.asyncio
async def test_existing_config_is_never_reseeded(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"admin_password": "mine", "mcp_auth_token": "tok"}))
    os.chmod(cfg_path, 0o600)
    store = ConfigStore(cfg_path)
    loaded = await store.load()
    assert loaded["admin_password"] == "mine"
    assert loaded["mcp_auth_token"] == "tok"
    # defaults are merged in for keys the file omits
    assert loaded["tools"]["permissions"] == {"allowed_tools": ["*"], "denied_tools": []}
