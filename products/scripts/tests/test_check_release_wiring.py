"""The release wiring check must reject a Quadlet that can select a mutable tag."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "check-release-wiring.py"


def load(tmp_path: Path, monkeypatch):
    spec = importlib.util.spec_from_file_location("release_wiring", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def write_tree(root: Path, *, image_version: str = "paas-0000000-podman.1",
               extra_quadlet: str = "") -> None:
    headscale = root / "deploy/headscale"
    (headscale / "quadlet").mkdir(parents=True)
    (headscale / "scripts").mkdir()
    (root / "deploy/catalog.json").write_text(json.dumps({
        "paas_source_commit": "0" * 40,
        "image_version": image_version,
    }))
    (headscale / "VERSION").write_text("0.1.3-podman.1\n")
    (headscale / "quadlet/headscale-mcp-admin.container").write_text(
        "[Container]\nImage=@@IMAGE@@\n" + extra_quadlet
    )
    (headscale / "quadlet/render-vars").write_text("MCP_ADMIN_PORT\nIMAGE\n")
    (headscale / "scripts/install.sh").write_text(
        'mcp_release_gate "$APP" headscale\nIMAGE=$IMAGE\n'
    )


def test_wired_tree_passes(tmp_path: Path, monkeypatch):
    module = load(tmp_path, monkeypatch)
    write_tree(tmp_path)

    assert module.check() == {
        "fork_base": "0" * 40,
        "headscale_version": "0.1.3-podman.1",
        "shared_image_version": "paas-0000000-podman.1",
    }


@pytest.mark.parametrize("second_image", [
    "Image=localhost/woow-headscale-mcp-admin:0.1.3-podman.1",
    " Image=localhost/woow-headscale-mcp-admin:0.1.3-podman.1",
    "\tImage = localhost/woow-headscale-mcp-admin:0.1.3-podman.1",
])
def test_signed_image_cannot_coexist_with_mutable_tag(
    tmp_path: Path, monkeypatch, second_image: str,
):
    module = load(tmp_path, monkeypatch)
    write_tree(tmp_path, extra_quadlet=second_image + "\n")

    with pytest.raises(ValueError, match="exactly one signed image"):
        module.check()


def test_shared_image_version_must_name_the_fork_base(tmp_path: Path, monkeypatch):
    module = load(tmp_path, monkeypatch)
    write_tree(tmp_path, image_version="paas-1111111-podman.1")

    with pytest.raises(ValueError, match="fork base"):
        module.check()
