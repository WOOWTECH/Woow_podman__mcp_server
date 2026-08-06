"""The SPA must be found relative to the package, never to the cwd.

``create_app`` defaults ``static_dir`` to ``Path.cwd() / "static"``. The
Dockerfile copies the built Vite bundle to ``podman_mcp_admin/static/`` and the
image's WORKDIR is ``/app``, so that default resolves to ``/app/static`` — a
path that does not exist in the shipped image. When it misses, the factory
simply logs a debug line and skips the mount: no ``/assets`` mount and, more
importantly, no SPA catch-all, so ``/`` answers 404 and the console is a blank
page while every API route and ``/healthz`` still work perfectly.

That is a silent, total GUI outage produced by a *missing* argument, and it was
caught only by hash-comparing a deployed container against the repository. The
behavioural test below fails if the resolution ever goes back to being
cwd-relative; the wiring test fails if ``main.py`` stops passing the argument.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcp_admin_core import app as app_mod


class _NoChildManager:
    """The factory's lifespan drives the process manager; keep it inert."""

    async def start(self) -> bool:
        return True

    async def stop(self) -> None:
        return None

    async def is_ready(self) -> bool:
        return True


@pytest.fixture
def no_child(monkeypatch):
    monkeypatch.setattr(app_mod, "get_process_manager", lambda: _NoChildManager())


def test_spa_is_served_from_an_explicit_dir_regardless_of_cwd(
    tmp_path: Path, monkeypatch, no_child
) -> None:
    """An explicit ``static_dir`` must win over the process's cwd.

    The cwd is moved somewhere with no ``static/`` at all, which is exactly the
    shipped image's situation.
    """
    bundle = tmp_path / "bundle"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html><title>console</title>")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert not (Path.cwd() / "static").exists()

    app = app_mod.create_app(title="test", static_dir=bundle)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "console" in response.text


def test_without_static_dir_the_spa_silently_disappears(
    tmp_path: Path, monkeypatch, no_child
) -> None:
    """Pin the failure mode, so the reason for the argument stays visible.

    This is not asserting desired behaviour — it documents why omitting
    ``static_dir`` is a GUI outage rather than a cosmetic default, and it will
    start failing if the factory ever learns to find the bundle on its own.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    app = app_mod.create_app(title="test")
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 404


def test_main_passes_an_explicit_static_dir_inside_the_package() -> None:
    """``main.py`` must keep passing it.

    Asserted against the source rather than by importing the module: importing
    ``podman_mcp_admin.main`` runs ``ensure_seeded()`` at module scope, which
    writes a real config file.
    """
    source = (
        Path(__file__).resolve().parents[1] / "podman_mcp_admin" / "main.py"
    ).read_text(encoding="utf-8")

    assert "static_dir=_STATIC_DIR" in source, "main.py stopped passing static_dir"
    assert '_STATIC_DIR = Path(__file__).parent / "static"' in source, (
        "the bundle path must be resolved from the package, not the cwd"
    )


def test_dockerfile_puts_the_bundle_where_main_looks_for_it() -> None:
    """The two halves of this contract live in different files.

    ``main.py`` points at ``podman_mcp_admin/static``; the Dockerfile decides
    where the built bundle lands. If either moves without the other, the GUI
    404s in the image while every test that stubs a directory still passes.
    """
    dockerfile = (
        Path(__file__).resolve().parents[1] / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "./podman_mcp_admin/static/" in dockerfile, (
        "Dockerfile no longer copies the SPA to the path main.py resolves"
    )


def test_repo_layout_matches_the_resolved_bundle_path() -> None:
    """``Path(__file__).parent / 'static'`` must name a real package subdir."""
    package = Path(__file__).resolve().parents[1] / "podman_mcp_admin"
    assert package.is_dir()
    # The bundle itself is a build artifact and is absent from a clean checkout;
    # what matters is that the *package* is where main.py anchors from.
    assert (package / "main.py").is_file()
    assert os.path.basename(str(package)) == "podman_mcp_admin"
