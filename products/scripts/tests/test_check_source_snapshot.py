"""The release snapshot must reject a Quadlet that can select a mutable tag."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "check-source-snapshot.py"


@pytest.mark.parametrize("second_image", [
    "Image=localhost/woow-headscale-mcp-admin:0.1.3-podman.1",
    " Image=localhost/woow-headscale-mcp-admin:0.1.3-podman.1",
    "\tImage = localhost/woow-headscale-mcp-admin:0.1.3-podman.1",
])
def test_signed_image_cannot_coexist_with_mutable_tag(
    tmp_path: Path, monkeypatch, second_image: str,
):
    spec = importlib.util.spec_from_file_location("source_snapshot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)

    deploy = tmp_path / "deploy"
    quadlet = deploy / "headscale/quadlet"
    quadlet.mkdir(parents=True)
    (deploy / "catalog.json").write_text(json.dumps({
        "paas_source_commit": "0" * 40,
        "image_version": "paas-0000000-podman.1",
    }))
    (deploy / "headscale/VERSION").write_text("0.1.3-podman.1\n")
    (quadlet / "headscale-mcp-admin.container").write_text(
        "[Container]\nImage=@@IMAGE@@\n" + second_image + "\n"
    )

    with pytest.raises(ValueError, match="exactly one signed image"):
        module.snapshot()
