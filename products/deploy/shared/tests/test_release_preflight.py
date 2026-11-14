"""The public release preflight refuses installs without independent proof."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
PREFLIGHT = ROOT / "products/deploy/release_preflight.py"


class ReleasePreflightTests(unittest.TestCase):
    def git(self, repo: Path, *args: str) -> str:
        env = os.environ.copy()
        env.update(GIT_AUTHOR_NAME="Release Test", GIT_AUTHOR_EMAIL="release@example.invalid",
                   GIT_COMMITTER_NAME="Release Test", GIT_COMMITTER_EMAIL="release@example.invalid")
        return subprocess.check_output(("git", "-C", str(repo), *args), env=env, text=True).strip()

    def fixture(self, base: Path):
        source, repo = base / "canonical", base / "podman"
        for directory in (source, repo):
            directory.mkdir()
            self.git(directory, "init", "-q")
        canonical_main = source / "apps/headscale/headscale_mcp_admin/main.py"
        canonical_main.parent.mkdir(parents=True)
        canonical_main.write_text('deployment_kind="paas"\n')
        emqx_main = source / "apps/emqx/emqx_mcp_admin/main.py"
        emqx_main.parent.mkdir(parents=True)
        emqx_main.write_text('deployment_kind="paas"\n')
        core = source / "packages/mcp-admin-core/mcp_admin_core/example.py"
        core.parent.mkdir(parents=True)
        core.write_text("canonical core\n")
        self.git(source, "add", ".")
        self.git(source, "commit", "-qm", "canonical fixture")
        canonical_commit = self.git(source, "rev-parse", "HEAD")
        for relative in ("apps/headscale/headscale_mcp_admin/main.py",
                         "apps/emqx/emqx_mcp_admin/main.py",
                         "packages/mcp-admin-core/mcp_admin_core/example.py"):
            target = repo / "products" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = (source / relative).read_text()
            target.write_text(data.replace('deployment_kind="paas"', 'deployment_kind="podman"'))
        scripts = repo / "products/scripts"
        scripts.mkdir(parents=True)
        for name in ("check-all-parity.py", "check-headscale-parity.py"):
            shutil.copy2(ROOT / "products/scripts" / name, scripts / name)
        catalog = repo / "products/deploy/catalog.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({
            "paas_source_commit": canonical_commit,
            "image_version": "test-release.1",
            "products": {"emqx": {"default_port": 18501}, "headscale": {"default_port": 18081}},
        }))
        version = repo / "products/deploy/headscale/VERSION"
        version.parent.mkdir(parents=True)
        version.write_text("test-headscale.1\n")
        executable = repo / "products/deploy/test-release-tool"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "Podman fixture")
        podman_commit = self.git(repo, "rev-parse", "HEAD")
        # As if fetched after the merge; only the unsigned path reads it.
        self.git(repo, "update-ref", "refs/remotes/origin/main", podman_commit)
        return source, repo, canonical_commit, podman_commit

    def archive_hash(self, repo: Path, commit: str) -> str:
        return hashlib.sha256(subprocess.check_output(
            ("git", "-C", str(repo), "-c", "tar.umask=0000",
             "archive", "--format=tar", commit))).hexdigest()

    def signed_release(self, base: Path, source: Path, repo: Path,
                       canonical_commit: str, podman_commit: str) -> tuple[Path, Path, Path]:
        image_id = "sha256:" + "a" * 64
        labels = {
            "org.opencontainers.image.revision": podman_commit,
            "io.woowtech.canonical-source": canonical_commit,
            "io.woowtech.release-build": "reviewed-build-001",
        }
        images = {}
        for product, version in (("emqx", "test-release.1"), ("headscale", "test-headscale.1")):
            images[product] = {"linux/amd64": {
                "reference": f"localhost/woow-{product}-mcp-admin:{version}",
                "image_id": image_id, "build_id": "reviewed-build-001", "labels": labels,
            }}
        manifest_data = {
            "schema": 1,
            "canonical": {"repository": "WOOWTECH/woow-mcp-server",
                          "commit": canonical_commit,
                          "archive_sha256": self.archive_hash(source, canonical_commit)},
            "podman": {"commit": podman_commit,
                       "archive_sha256": self.archive_hash(repo, podman_commit)},
            "images": images,
        }
        manifest = base / "release.json"
        manifest.write_text(json.dumps(manifest_data, sort_keys=True))
        key, public = base / "test-private.pem", base / "test-public.pem"
        subprocess.run(("openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(key)),
                       check=True, capture_output=True)
        subprocess.run(("openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)),
                       check=True, capture_output=True)
        subprocess.run(("openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
                        "-in", str(manifest), "-out", f"{manifest}.sig"),
                       check=True, capture_output=True)
        image = base / "image.json"
        image.write_text(json.dumps({"Id": image_id, "Os": "linux", "Architecture": "amd64",
                                     "Labels": labels}))
        bin_dir = base / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "podman"
        shim.write_text('#!/bin/sh\n[ "$1 $2 $3" = "image inspect --format" ] || exit 9\ncat "$RELEASE_TEST_IMAGE"\n')
        shim.chmod(0o755)
        return manifest, public, image

    def verify(self, repo: Path, source: Path, manifest: Path, public: Path, image: Path,
               product: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PATH"] = f"{manifest.parent / 'bin'}:{env['PATH']}"
        env["RELEASE_TEST_IMAGE"] = str(image)
        # Exercise the actual verifier with an isolated test key. The installer
        # and public CLI intentionally have no trust-anchor override.
        probe = (
            "import importlib.util, pathlib, sys\n"
            "spec=importlib.util.spec_from_file_location('release_preflight',sys.argv[1])\n"
            "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
            "try:\n"
            " print(module.verify(*(pathlib.Path(p) for p in sys.argv[2:6]),sys.argv[6]))\n"
            "except (module.ReleaseError,OSError,KeyError,TypeError) as error:\n"
            " print(f'release preflight: {error}',file=sys.stderr);sys.exit(1)\n"
        )
        return subprocess.run(
            [sys.executable, "-c", probe, str(PREFLIGHT), str(repo), str(source),
             str(manifest), str(public), product],
            env=env, capture_output=True, text=True,
        )

    def resign(self, manifest: Path) -> None:
        subprocess.run(("openssl", "pkeyutl", "-sign", "-inkey",
                        str(manifest.parent / "test-private.pem"), "-rawin", "-in",
                        str(manifest), "-out", f"{manifest}.sig"),
                       check=True, capture_output=True)

    def test_missing_reviewed_manifest_fails_before_image_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            result = subprocess.run(
                [sys.executable, str(PREFLIGHT), "--repo", str(ROOT),
                 "--product", "emqx", "--release-manifest", str(base / "missing.json"),
                 "--source-checkout", str(base / "source")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release manifest", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_public_cli_rejects_a_caller_supplied_trust_anchor(self):
        result = subprocess.run(
            [sys.executable, str(PREFLIGHT), "--repo", str(ROOT),
             "--product", "emqx", "--release-manifest", "/nonexistent/release.json",
             "--source-checkout", "/nonexistent/source",
             "--trust-key", "/tmp/fake-release.pub"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments: --trust-key", result.stderr)

    def test_root_owned_mode_bits_do_not_hide_a_trust_anchor_writable_by_deployer(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, repo_commit = self.fixture(base)
            manifest, public, _ = self.signed_release(base, source, repo,
                                                       source_commit, repo_commit)
            spec = __import__("importlib.util").util.spec_from_file_location(
                "release_preflight_acl_test", PREFLIGHT)
            module = __import__("importlib.util").util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.TRUST_KEY = public
            original_lstat = Path.lstat

            def root_owned_mode(path):
                metadata = original_lstat(path)
                return SimpleNamespace(st_uid=0, st_mode=metadata.st_mode & ~0o022)

            self.assertTrue(os.access(public, os.W_OK))
            with mock.patch.object(Path, "lstat", root_owned_mode):
                with self.assertRaisesRegex(module.ReleaseError, "trust anchor.*read-only"):
                    module.signed_manifest(manifest, public)

    def test_archive_identity_ignores_global_tar_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "gitconfig"
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
                config.write_text("[tar]\n\tumask = 0002\n")
                source, repo, source_commit, repo_commit = self.fixture(base)
                manifest, public, image = self.signed_release(
                    base, source, repo, source_commit, repo_commit)
                config.write_text("[tar]\n\tumask = 0022\n")
                result = self.verify(repo, source, manifest, public, image, "headscale")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_signed_manifest_matching_source_and_exact_image_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, repo_commit = self.fixture(base)
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            for product in ("emqx", "headscale"):
                with self.subTest(product=product):
                    result = self.verify(repo, source, manifest, public, image, product)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "sha256:" + "a" * 64)

    def test_changed_manifest_without_reviewer_signature_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, repo_commit = self.fixture(base)
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            data = json.loads(manifest.read_text())
            data["images"]["emqx"]["linux/amd64"]["image_id"] = "sha256:" + "b" * 64
            manifest.write_text(json.dumps(data))
            result = self.verify(repo, source, manifest, public, image, "emqx")
            self.assertIn("signature is invalid", result.stderr)

    def test_forged_matching_labels_cannot_authorize_wrong_image_id(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, repo_commit = self.fixture(base)
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            actual = json.loads(image.read_text())
            actual["Id"] = "sha256:" + "b" * 64
            image.write_text(json.dumps(actual))
            result = self.verify(repo, source, manifest, public, image, "emqx")
            self.assertIn("unreviewed image ID", result.stderr)

    def test_wrong_tag_is_rejected_even_with_signed_image_id(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, repo_commit = self.fixture(base)
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            data = json.loads(manifest.read_text())
            data["images"]["emqx"]["linux/amd64"]["reference"] = "localhost/woow-emqx-mcp-admin:other"
            manifest.write_text(json.dumps(data))
            self.resign(manifest)
            result = self.verify(repo, source, manifest, public, image, "emqx")
            self.assertIn("reference differs", result.stderr)

    def test_wrong_catalog_pin_is_rejected_even_with_valid_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, _ = self.fixture(base)
            catalog = repo / "products/deploy/catalog.json"
            data = json.loads(catalog.read_text())
            data["paas_source_commit"] = "0" * 40
            catalog.write_text(json.dumps(data))
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-qm", "wrong pin fixture")
            repo_commit = self.git(repo, "rev-parse", "HEAD")
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            result = self.verify(repo, source, manifest, public, image, "emqx")
            self.assertIn("catalog pin differs", result.stderr)

    def test_signed_manifest_cannot_accept_missing_or_unreviewed_image_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, repo_commit = self.fixture(base)
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            inspected = json.loads(image.read_text())
            inspected["Labels"]["io.woowtech.release-build"] = "forged-build-001"
            image.write_text(json.dumps(inspected))
            result = self.verify(repo, source, manifest, public, image, "headscale")
            self.assertIn("labels differ", result.stderr)

    def test_changed_vendored_content_is_rejected_even_with_new_signed_podman_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, _ = self.fixture(base)
            (repo / "products/packages/mcp-admin-core/mcp_admin_core/example.py").write_text(
                "unreviewed core\n")
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-qm", "changed vendor fixture")
            repo_commit = self.git(repo, "rev-parse", "HEAD")
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            result = self.verify(repo, source, manifest, public, image, "emqx")
            self.assertIn("source parity failed", result.stderr)

    def test_dirty_canonical_checkout_is_rejected_even_when_pinned_object_is_signed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, repo_commit = self.fixture(base)
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            (source / "apps/emqx/emqx_mcp_admin/main.py").write_text('deployment_kind="spoofed"\n')
            result = self.verify(repo, source, manifest, public, image, "emqx")
            self.assertIn("canonical checkout has unreviewed local changes", result.stderr)

    def test_different_canonical_head_is_rejected_even_when_pinned_object_is_signed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, repo, source_commit, repo_commit = self.fixture(base)
            manifest, public, image = self.signed_release(base, source, repo,
                                                           source_commit, repo_commit)
            self.git(source, "commit", "--allow-empty", "-qm", "different HEAD")
            result = self.verify(repo, source, manifest, public, image, "emqx")
            self.assertIn("canonical checkout HEAD differs", result.stderr)

    def local_image(self, base: Path, podman_commit: str, canonical_commit: str,
                    overrides: dict[str, str] | None = None) -> dict[str, str]:
        """A local image for the owner-approved unsigned path, and the env that inspects it."""
        image_labels = {
            "org.opencontainers.image.revision": podman_commit,
            "io.woowtech.canonical-source": canonical_commit,
            "io.woowtech.release-build": "local-build-0001",
        }
        image_labels.update(overrides or {})
        image = base / "image.json"
        image.write_text(json.dumps({"Id": "c" * 64, "Os": "linux", "Architecture": "amd64",
                                     "Labels": image_labels}))
        bin_dir = base / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "podman"
        shim.write_text('#!/bin/sh\n[ "$1 $2 $3" = "image inspect --format" ] || exit 9\ncat "$RELEASE_TEST_IMAGE"\n')
        shim.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["RELEASE_TEST_IMAGE"] = str(image)
        return env

    def unsigned(self, repo: Path, product: str, env: dict[str, str],
                 *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PREFLIGHT), "--repo", str(repo), "--product", product,
             "--unsigned", *extra],
            env=env, capture_output=True, text=True,
        )

    def test_unsigned_release_accepts_an_image_built_from_this_clean_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _, repo, source_commit, repo_commit = self.fixture(base)
            env = self.local_image(base, repo_commit, source_commit)
            for product in ("emqx", "headscale"):
                with self.subTest(product=product):
                    result = self.unsigned(repo, product, env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "sha256:" + "c" * 64)

    def test_unsigned_release_refuses_a_checkout_with_local_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _, repo, source_commit, repo_commit = self.fixture(base)
            env = self.local_image(base, repo_commit, source_commit)
            (repo / "products/deploy/unreviewed.txt").write_text("not committed\n")
            result = self.unsigned(repo, "emqx", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Podman checkout has local changes", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_unsigned_release_refuses_an_image_from_another_commit_pin_or_build(self):
        cases = (
            ({"org.opencontainers.image.revision": "0" * 40}, "not built from this Podman checkout"),
            ({"io.woowtech.canonical-source": "0" * 40}, "another canonical pin"),
            ({"io.woowtech.release-build": "short"}, "no release build label"),
        )
        for labels, message in cases:
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    _, repo, source_commit, repo_commit = self.fixture(base)
                    env = self.local_image(base, repo_commit, source_commit, labels)
                    result = self.unsigned(repo, "headscale", env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_unsigned_release_refuses_a_commit_that_is_not_on_origin_main(self):
        for case in ("local commit", "no origin/main"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    _, repo, source_commit, repo_commit = self.fixture(base)
                    if case == "local commit":
                        self.git(repo, "commit", "--allow-empty", "-qm", "not pushed")
                        repo_commit = self.git(repo, "rev-parse", "HEAD")
                    else:
                        self.git(repo, "update-ref", "-d", "refs/remotes/origin/main")
                    env = self.local_image(base, repo_commit, source_commit)
                    result = self.unsigned(repo, "emqx", env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("not on origin/main", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_unsigned_release_takes_no_manifest_and_signed_release_still_needs_one(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            mixed = self.unsigned(ROOT, "emqx", os.environ.copy(),
                                  "--release-manifest", str(base / "release.json"))
            self.assertNotEqual(mixed.returncode, 0)
            self.assertIn("--unsigned takes no --release-manifest", mixed.stderr)
            bare = subprocess.run(
                [sys.executable, str(PREFLIGHT), "--repo", str(ROOT), "--product", "emqx"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(bare.returncode, 0)
            self.assertIn("--release-manifest and --source-checkout are required", bare.stderr)


if __name__ == "__main__":
    unittest.main()
