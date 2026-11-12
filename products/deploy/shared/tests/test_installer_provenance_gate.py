"""Rootless CLI rejects unreviewed releases without changing installed state."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[4]
MANAGE = ROOT / "products/deploy/manage.sh"


class InstallerGateTests(unittest.TestCase):
    def env(self, base: Path) -> dict[str, str]:
        home = base / "home"
        home.mkdir(exist_ok=True)
        env = os.environ.copy()
        env.update(HOME=str(home), QL_STATE_ROOT=str(home / "state"),
                   QL_QUADLET_DIR=str(home / "units"))
        return env

    def install_args(self, base: Path, product: str, action: str) -> list[str]:
        return [str(MANAGE), product, action,
                "--release-manifest", str(base / "absent.json"),
                "--source-checkout", str(base / "absent-source")]

    def preflight_shim(self, base: Path, env: dict[str, str], fail_at: int = 0) -> None:
        """Only the test process substitutes the external verifier command."""
        bin_dir = base / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "python3"
        behaviour = (
            'case "$1" in\n'
            '  */release_preflight.py)\n'
            '    n=0; [ -f "$RELEASE_TEST_COUNT" ] && n=$(cat "$RELEASE_TEST_COUNT")\n'
            '    n=$((n + 1)); printf "%s" "$n" > "$RELEASE_TEST_COUNT"\n'
            + (f'    [ "$n" -lt {fail_at} ] || exit 7\n' if fail_at else '')
            + '    printf "sha256:%064d\\n" 0; exit 0 ;;\n'
            '  *) exec /usr/bin/python3 "$@" ;;\n'
            'esac\n'
        )
        shim.write_text("#!/bin/sh\n" + behaviour)
        shim.chmod(0o755)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["RELEASE_TEST_COUNT"] = str(base / "preflight-count")

    def test_missing_manifest_leaves_no_persistent_state_for_install_or_upgrade(self):
        for product in ("emqx", "headscale"):
            for action in ("install", "upgrade"):
                with self.subTest(product=product, action=action):
                    with tempfile.TemporaryDirectory() as directory:
                        base = Path(directory)
                        env = self.env(base)
                        home = Path(env["HOME"])
                        result = subprocess.run(
                            self.install_args(base, product, action),
                            env=env, capture_output=True, text=True,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("release", result.stderr.lower())
                        self.assertFalse((home / ".config").exists())
                        self.assertFalse((home / "units").exists())
                        self.assertFalse((home / "state").exists())

    def test_changed_proof_under_lock_cleans_new_empty_state_directory(self):
        for product in ("emqx", "headscale"):
            for action in ("install", "upgrade"):
                with self.subTest(product=product, action=action):
                    with tempfile.TemporaryDirectory() as directory:
                        base = Path(directory)
                        env = self.env(base)
                        self.preflight_shim(base, env, fail_at=2)
                        result = subprocess.run(self.install_args(base, product, action),
                                                env=env, capture_output=True, text=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("changed while acquiring", result.stderr)
                        self.assertEqual((base / "preflight-count").read_text(), "2")
                        self.assertFalse((Path(env["HOME"]) / "state").exists())
                        self.assertFalse((Path(env["HOME"]) / ".config").exists())

    def test_final_recheck_failure_releases_lock_and_cleans_new_state(self):
        library = ROOT / "scripts/lib/quadlet-lib.sh"
        gate = ROOT / "products/deploy/release-gate.sh"
        for product in ("emqx", "headscale"):
            with self.subTest(product=product):
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    env = self.env(base)
                    self.preflight_shim(base, env, fail_at=3)
                    script = (
                        'set -euo pipefail; REPO=$1; source "$REPO/scripts/lib/quadlet-lib.sh"; '
                        'source "$REPO/products/deploy/release-gate.sh"; '
                        'mcp_release_gate "$2" "$3" "$4" "$5"; '
                        'mcp_release_recheck "$2" "$3" "$4" "$5"'
                    )
                    result = subprocess.run(
                        ["bash", "-c", script, "gate-test", str(ROOT), f"{product}-mcp-admin",
                         product, str(base / "absent.json"), str(base / "absent-source")],
                        env=env, capture_output=True, text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("before persistent install", result.stderr)
                    self.assertEqual((base / "preflight-count").read_text(), "3")
                    self.assertFalse((Path(env["HOME"]) / "state").exists())

    def test_active_service_with_different_image_requires_upgrade(self):
        for product in ("emqx", "headscale"):
            with self.subTest(product=product):
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    env = self.env(base)
                    self.preflight_shim(base, env)
                    unit = Path(env["QL_QUADLET_DIR"]) / f"{product}-mcp-admin.container"
                    unit.parent.mkdir()
                    unit.write_text("[Container]\nImage=sha256:" + "b" * 64 + "\n")
                    systemctl = base / "bin/systemctl"
                    systemctl.write_text('#!/bin/sh\n[ "$1 $2 $3" = "--user is-active --quiet" ]\n')
                    systemctl.chmod(0o755)
                    result = subprocess.run(self.install_args(base, product, "install"),
                                            env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("use upgrade", result.stderr)
                    self.assertEqual(unit.read_text(), "[Container]\nImage=sha256:" + "b" * 64 + "\n")
                    self.assertFalse((Path(env["HOME"]) / ".config").exists())

    def test_invalid_upgrade_preserves_existing_manifest_and_unit_without_backup(self):
        for product in ("emqx", "headscale"):
            with self.subTest(product=product):
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    env = self.env(base)
                    home = Path(env["HOME"])
                    state = home / "state" / f"{product}-mcp-admin"
                    state.mkdir(parents=True)
                    manifest = state / "manifest"
                    manifest.write_text("existing manifest\n")
                    unit = home / "units" / f"{product}-mcp-admin.container"
                    unit.parent.mkdir(parents=True)
                    unit.write_text("existing unit\n")
                    result = subprocess.run(self.install_args(base, product, "upgrade"),
                                            env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(manifest.read_text(), "existing manifest\n")
                    self.assertEqual(unit.read_text(), "existing unit\n")
                    self.assertFalse((state / "rollback").exists())

    def test_concurrent_install_cannot_take_another_process_lock(self):
        library = ROOT / "scripts/lib/quadlet-lib.sh"
        for product in ("emqx", "headscale"):
            with self.subTest(product=product):
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    env = self.env(base)
                    self.preflight_shim(base, env)
                    app = f"{product}-mcp-admin"
                    marker = base / "ready"
                    holder = subprocess.Popen(
                        ["bash", "-c", 'source "$1"; ql_lock "$2"; touch "$3"; sleep 2',
                         "lock-holder", str(library), app, str(marker)],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    )
                    try:
                        deadline = time.monotonic() + 3
                        while not marker.exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(marker.exists(), "lock holder did not start")
                        result = subprocess.run(self.install_args(base, product, "install"),
                                                env=env, capture_output=True, text=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("another install/upgrade/uninstall", result.stderr)
                        self.assertFalse((Path(env["HOME"]) / ".config").exists())
                    finally:
                        holder.terminate()
                        holder.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
