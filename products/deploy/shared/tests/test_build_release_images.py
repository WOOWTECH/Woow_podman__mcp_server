"""Release images are built from committed source and carry the labels the preflight checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[4]
BUILD = ROOT / "products/deploy/build-release-images.sh"
PIN = "1" * 40


class BuildReleaseImagesTests(unittest.TestCase):
    def git(self, repo: Path, *args: str) -> str:
        env = os.environ.copy()
        env.update(GIT_AUTHOR_NAME="Release Test", GIT_AUTHOR_EMAIL="release@example.invalid",
                   GIT_COMMITTER_NAME="Release Test", GIT_COMMITTER_EMAIL="release@example.invalid")
        return subprocess.check_output(("git", "-C", str(repo), *args), env=env, text=True).strip()

    def fixture(self, base: Path) -> tuple[Path, dict[str, str]]:
        repo = base / "podman"
        deploy = repo / "products/deploy"
        (deploy / "shared").mkdir(parents=True)
        shutil.copy2(BUILD, deploy / BUILD.name)
        shutil.copy2(ROOT / "products/deploy/shared/catalog.py", deploy / "shared/catalog.py")
        (deploy / "catalog.json").write_text(json.dumps({
            "paas_source_commit": PIN,
            "image_version": "paas-1111111-podman.1",
            "products": {
                "headscale": {"package": "woow-headscale-mcp-server",
                              "module": "headscale_mcp_admin.main:app", "default_port": 18081},
                "emqx": {"package": "woow-emqx-mcp-server",
                         "module": "emqx_mcp_admin.main:app", "default_port": 18501},
            },
        }))
        (deploy / "headscale").mkdir()
        (deploy / "headscale/VERSION").write_text("0.9.0-podman.1\n")
        (deploy / "headscale/Dockerfile").write_text("FROM scratch\n")
        (repo / "products/apps/emqx").mkdir(parents=True)
        (repo / "products/apps/emqx/Dockerfile").write_text("FROM scratch\n")
        (repo / ".gitignore").write_text("products/ignored-marker\n")
        self.git(repo, "init", "-q")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "build fixture")
        self.git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        # Ignored files exist in the worktree but must never reach a build context.
        (repo / "products/ignored-marker").write_text("local only\n")
        bin_dir = base / "bin"
        bin_dir.mkdir()
        (bin_dir / "podman").write_text(
            '#!/bin/sh\n'
            'if [ "$1" = build ]; then\n'
            '  printf "%s\\n" "$*" >> "$BUILD_TEST_LOG"\n'
            '  for last; do :; done\n'
            '  [ -e "$last/ignored-marker" ] && echo LEAKED >> "$BUILD_TEST_LOG"\n'
            '  [ -f "$last/apps/emqx/Dockerfile" ] || echo NO-SOURCE >> "$BUILD_TEST_LOG"\n'
            '  exit 0\n'
            'fi\n'
            '[ "$1 $2 $3 $4" = "image inspect --format {{.Id}}" ] || exit 9\n'
            'echo sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\n')
        (bin_dir / "podman").chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["BUILD_TEST_LOG"] = str(base / "builds.log")
        return repo, env

    def build(self, repo: Path, env: dict[str, str], *products: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(repo / "products/deploy" / BUILD.name), *products],
                              env=env, capture_output=True, text=True)

    def test_every_catalog_product_is_built_from_the_committed_tree_with_release_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, env = self.fixture(base)
            revision = self.git(repo, "rev-parse", "HEAD")
            result = self.build(repo, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            builds = (base / "builds.log").read_text().splitlines()
            self.assertEqual(len(builds), 2, builds)
            headscale, emqx = builds
            for expected in (f"--build-arg VCS_REF={revision}", f"--build-arg CANONICAL_SOURCE={PIN}",
                             "--build-arg RELEASE_BUILD_ID=local-",
                             "-t localhost/woow-headscale-mcp-admin:0.9.0-podman.1",
                             "/products/deploy/headscale/Dockerfile"):
                self.assertIn(expected, headscale)
            for expected in (f"--label org.opencontainers.image.revision={revision}",
                             f"--label io.woowtech.canonical-source={PIN}",
                             "--label io.woowtech.release-build=local-",
                             "-t localhost/woow-emqx-mcp-admin:paas-1111111-podman.1",
                             "/products/apps/emqx/Dockerfile"):
                self.assertIn(expected, emqx)
            self.assertNotIn(str(repo), headscale + emqx)
            self.assertIn("built emqx localhost/woow-emqx-mcp-admin:paas-1111111-podman.1 sha256:",
                          result.stdout)

    def test_a_commit_that_is_not_on_origin_main_builds_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, env = self.fixture(base)
            self.git(repo, "commit", "--allow-empty", "-qm", "not pushed")
            result = self.build(repo, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not on origin/main", result.stderr)
            self.assertFalse((base / "builds.log").exists())

    def test_local_changes_or_an_unknown_product_build_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, env = self.fixture(base)
            unknown = self.build(repo, env, "emqx", "vk")
            self.assertNotEqual(unknown.returncode, 0)
            self.assertIn("unknown catalog product: vk", unknown.stderr)
            (repo / "products/apps/emqx/Dockerfile").write_text("FROM scratch\nRUN true\n")
            dirty = self.build(repo, env)
            self.assertNotEqual(dirty.returncode, 0)
            self.assertIn("local changes", dirty.stderr)
            self.assertFalse((base / "builds.log").exists())


if __name__ == "__main__":
    unittest.main()
