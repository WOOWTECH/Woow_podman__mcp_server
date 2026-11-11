"""The source gate must catch drift in the shared admin implementation."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "check-all-parity.py"
CORE = "packages/mcp-admin-core/mcp_admin_core"
CORE_FILE = f"{CORE}/auth/middleware.py"
CORE_PROJECT = "packages/mcp-admin-core/pyproject.toml"


def _git(source: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Parity Test", GIT_AUTHOR_EMAIL="parity@example.invalid",
        GIT_COMMITTER_NAME="Parity Test", GIT_COMMITTER_EMAIL="parity@example.invalid",
    )
    result = subprocess.run(
        ("git", "-C", str(source), *args), env=env, check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("changed_path", (CORE_FILE, CORE_PROJECT))
def test_pinned_source_detects_shared_core_drift(tmp_path, monkeypatch, capsys, changed_path):
    spec = importlib.util.spec_from_file_location("all_product_parity", SCRIPT)
    assert spec is not None and spec.loader is not None
    parity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parity)
    assert CORE in parity.PATHS
    assert CORE_PROJECT in parity.PATHS

    source = tmp_path / "source"
    vendored = tmp_path / "vendored"
    source.mkdir()
    vendored.mkdir()
    _git(source, "init", "-q")
    for relative_path in (CORE_FILE, CORE_PROJECT):
        for base in (source, vendored):
            target = base / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("pinned source\n")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "fixture source")

    monkeypatch.setattr(parity, "ROOT", vendored)
    monkeypatch.setattr(parity, "PIN", _git(source, "rev-parse", "HEAD"))
    monkeypatch.setattr(parity, "PATHS", (CORE, CORE_PROJECT))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--source-checkout", str(source)])
    assert parity.main() == 0
    capsys.readouterr()

    (vendored / changed_path).write_text("unpublished change\n")
    assert parity.main() == 1
    assert changed_path in capsys.readouterr().err
