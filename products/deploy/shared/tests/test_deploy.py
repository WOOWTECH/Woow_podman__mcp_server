"""Deployment contracts that must hold for every Quadlet MCP product."""

from __future__ import annotations

import importlib.util
import json
import os
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
        "nextcloud", "opendesign", "k8s",
    }
    assert len({item["default_port"] for item in catalog["products"].values()}) == len(names)
    assert "odoo-manage" not in names
    assert "vk" not in names


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


def test_quadlet_templates_cap_memory_and_stop_startup_failure_loops():
    for path in (SHARED / "quadlet/product.container",
                 SHARED.parent / "headscale/quadlet/headscale-mcp-admin.container"):
        text = path.read_text()
        assert "PodmanArgs=--memory=@@MCP_MEMORY_LIMIT@@" in text
        assert "RestartPreventExitStatus=3" in text
    for path in (SHARED / "quadlet/render-vars", SHARED.parent / "headscale/quadlet/render-vars"):
        assert "MCP_MEMORY_LIMIT" in path.read_text().split()


def test_runtime_settings_follow_the_core_rules():
    validate = module("validate_env").validate_runtime
    validate({"MCP_MEMORY_LIMIT": "1g"})
    validate({"MCP_MEMORY_LIMIT": "768m", "MCP_ADMIN_ORIGIN_MODE": "enforce",
              "MCP_ADMIN_ALLOWED_ORIGINS": "https://claude.ai, http://127.0.0.1:18501,https://[::1]:8443",
              "MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE": "16", "MCP_GATEWAY_MAX_STREAMS_PER_PROFILE": "15",
              "MCP_GATEWAY_POOL_TIMEOUT_SECONDS": "2.5"})
    for bad in ({"MCP_MEMORY_LIMIT": "256m"}, {"MCP_MEMORY_LIMIT": "1.5g"}, {"MCP_MEMORY_LIMIT": "1G"},
                {"MCP_ADMIN_ORIGIN_MODE": "Enforce"}, {"MCP_ADMIN_ORIGIN_MODE": "report_only"},
                {"MCP_ADMIN_ALLOWED_ORIGINS": "*"}, {"MCP_ADMIN_ALLOWED_ORIGINS": "null"},
                {"MCP_ADMIN_ALLOWED_ORIGINS": "https://claude.ai/"},
                {"MCP_ADMIN_ALLOWED_ORIGINS": "https://claude.ai,"},
                {"MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE": "1"},
                {"MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE": "8", "MCP_GATEWAY_MAX_STREAMS_PER_PROFILE": "8"},
                {"MCP_GATEWAY_POOL_TIMEOUT_SECONDS": "0"}, {"MCP_GATEWAY_POOL_TIMEOUT_SECONDS": "301"}):
        with pytest.raises(ValueError):
            validate(bad)


def test_origin_grammar_matches_the_vendored_core():
    checker = module("validate_env")
    core = (PRODUCTS / "packages/mcp-admin-core/mcp_admin_core/origin_security.py").read_text()
    for name, pattern in (("_ORIGIN", checker._ORIGIN), ("_LABEL", checker._LABEL)):
        assert f'{name} = re.compile(r"{pattern.pattern}", re.I | re.ASCII)' in core
    assert 'MODE_REPORT_ONLY = "report-only"' in core and 'MODE_ENFORCE = "enforce"' in core
    assert checker.ORIGIN_MODES == ("report-only", "enforce")


PROFILE = "0123456789abcdef01234567"


def seed_backup(path: Path) -> None:
    (path / "profiles" / PROFILE).mkdir(parents=True)
    common = {"admin_password": "backup-password-from-before", "admin_session_generation": "old-session",
              "mcp_auth_revoked": False, "mcp_auth_generation": "old-generation", "connection": {},
              "tools": {"disabled": [], "disabled_operations": {},
                        "owner_defaults_20261025": {"applied": "fresh_install"}},
              "token_history": [], "proxy": {}}
    default = {**common, "mcp_auth_token": "old-token-a", "mcp_server": {"port": 18001},
               "profile_index": {PROFILE: {"label": "B", "port": 18002}}}
    child = {**common, "mcp_auth_token": "old-token-b", "mcp_server": {"port": 18002}}
    for file, value in ((path / "config.json", default),
                        (path / "profiles" / PROFILE / "config.json", child)):
        file.write_text(json.dumps(value))
        file.chmod(0o600)


def test_restore_revoked_keeps_profiles_and_sets_the_installed_password(tmp_path):
    pytest.importorskip("mcp_admin_core.restore_guard")
    source, target = tmp_path / "backup", tmp_path / "volume"
    source.mkdir()
    target.mkdir(mode=0o755)
    seed_backup(source)
    module("restore-revoked").restore("emqx", source, target, "installed-secret-password")
    root = json.loads((target / "config.json").read_text())
    child = json.loads((target / "profiles" / PROFILE / "config.json").read_text())
    assert root["admin_password"] == "installed-secret-password"
    assert root["profile_index"] == {PROFILE: {"label": "B", "port": 18002}}
    for config in (root, child):
        assert config["mcp_auth_token"] == "" and config["mcp_auth_revoked"] is True
        assert config["admin_session_generation"] != "old-session"
        assert config["mcp_auth_generation"] != "old-generation"
    assert (target / "config.json").stat().st_mode & 0o777 == 0o600
    assert not (target / ".restore-revoked").exists()


def test_restore_revoked_refuses_a_weak_password_and_a_used_volume(tmp_path):
    pytest.importorskip("mcp_admin_core.restore_guard")
    restore = module("restore-revoked").restore
    source, target = tmp_path / "backup", tmp_path / "volume"
    source.mkdir()
    target.mkdir(mode=0o755)
    seed_backup(source)
    with pytest.raises(ValueError):
        restore("emqx", source, target, "short")
    (target / "config.json").write_text("{}")
    with pytest.raises(ValueError):
        restore("emqx", source, target, "installed-secret-password")


def test_restore_revoked_drops_k8s_derived_files_but_not_unknown_ones(tmp_path):
    guard = pytest.importorskip("mcp_admin_core.restore_guard")
    restore = module("restore-revoked").restore
    source, target = tmp_path / "backup", tmp_path / "volume"
    source.mkdir()
    target.mkdir(mode=0o755)
    seed_backup(source)
    for directory in (source, source / "profiles" / PROFILE):
        for name in ("k8s-mcp.toml", "kubeconfig"):
            (directory / name).write_text("rewritten from config.json on every child start")
            (directory / name).chmod(0o600)
    restore("k8s", source, target, "installed-secret-password")
    assert not list(target.rglob("k8s-mcp.toml")) and not list(target.rglob("kubeconfig"))
    assert json.loads((target / "config.json").read_text())["mcp_auth_revoked"] is True
    other, other_target = tmp_path / "other", tmp_path / "other-volume"
    other.mkdir()
    other_target.mkdir(mode=0o755)
    seed_backup(other)
    (other / "kubeconfig").write_text("not derived for this product")
    (other / "kubeconfig").chmod(0o600)
    with pytest.raises(guard.RestoreError):
        restore("emqx", other, other_target, "installed-secret-password")
    assert not any(other_target.iterdir())
