"""An accepted release still rolls back after a later installer failure.

The release verifier is exercised separately. This test uses a synthetic
post-verification installer failure, the real lifecycle and a disposable
rootless Podman volume; it never names a live product or Quadlet.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[4]


def command(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, check=True, **kwargs)


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip("\n"))
    path.chmod(0o700)


@unittest.skipUnless(shutil.which("podman"), "rootless Podman is required")
class ReleaseRollbackTests(unittest.TestCase):
    def test_shared_upgrade_restores_unit_volume_and_auth_after_later_failure(self):
        product = "qa_release_rollback_" + secrets.token_hex(4)
        app = product + "-mcp-admin"
        volume = app.replace("-", "_") + "_data"
        created = False
        try:
            with tempfile.TemporaryDirectory(prefix="qa-release-rollback-") as directory:
                root = Path(directory)
                lifecycle = root / "products/deploy/shared/lifecycle.sh"
                lifecycle_source = (ROOT / "products/deploy/shared/lifecycle.sh").read_text()
                default_backup = "${1:-$HOME/backups/$APP}"
                self.assertEqual(lifecycle_source.count(default_backup), 1)
                executable(lifecycle, lifecycle_source.replace(default_backup,
                    "${1:-$QA_BACKUP_ROOT}"))
                shutil.copy2(ROOT / "products/deploy/shared/rotate-restored-auth.py",
                             lifecycle.parent / "rotate-restored-auth.py")
                library = root / "scripts/lib/quadlet-lib.sh"
                library.parent.mkdir(parents=True)
                shutil.copy2(ROOT / "scripts/lib/quadlet-lib.sh", library)
                with library.open("a") as stream:
                    stream.write("\nql_require_rootless() { :; }\n"
                                 "ql_require_user_systemd() { :; }\n"
                                 "ql_lock() { :; }\n"
                                 "ql_wait_container_healthy() { :; }\n")
                executable(root / "products/deploy/release-gate.sh", """
                    #!/usr/bin/env bash
                    mcp_release_gate() { RELEASE_IMAGE_ID=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; }
                """)
                executable(root / "products/deploy/shared/catalog.py", """
                    #!/usr/bin/env python3
                    import os, sys
                    assert sys.argv[1:] == ['get', os.environ['QA_PRODUCT']]
                    sys.stdout.buffer.write(('\\0'.join(['qa-package', 'qa.main:app', '18099',
                                                          'qa-version', 'shared']) + '\\0').encode())
                """)
                old_unit = root / "units/old.container"
                new_unit = root / "units/new.container"
                old_unit.parent.mkdir()
                old_unit.write_text("Image=synthetic-old\n")
                old_sha = hashlib.sha256(old_unit.read_bytes()).hexdigest()
                state = root / "state" / app
                state.mkdir(parents=True)
                original_manifest = f"oldhash  {old_unit}\n"
                (state / "manifest").write_text(original_manifest)
                executable(root / "products/deploy/manage.sh", """
                    #!/usr/bin/env bash
                    set -euo pipefail
                    [[ $1 == "$QA_PRODUCT" ]]
                    if [[ $2 == smoke ]]; then exit 0; fi
                    [[ $2 == install && $3 == --release-manifest && $5 == --source-checkout ]]
                    printf 'Image=synthetic-new\n' > "$QA_OLD_UNIT"
                    printf 'Image=synthetic-extra\n' > "$QA_NEW_UNIT"
                    printf 'newhash  %s\nextrahash  %s\n' "$QA_OLD_UNIT" "$QA_NEW_UNIT" > "$QA_STATE/manifest"
                    mountpoint=$(podman volume inspect --format '{{.Mountpoint}}' "$QA_VOLUME")
                    printf 'mutated\n' > "$mountpoint/payload.txt"
                    printf '{"admin_password":"synthetic-only-password","admin_session_generation":"mutated"}\n' > "$mountpoint/config.json"
                    exit 7
                """)
                executable(root / "bin/systemctl", """
                    #!/usr/bin/env python3
                    import os, pathlib, subprocess, sys
                    args = sys.argv[1:]
                    if args and args[0] == '--user': args = args[1:]
                    state = pathlib.Path(os.environ['QA_SYSTEMD_STATE'])
                    action = args[0]
                    if action == 'is-active':
                        raise SystemExit(0 if state.read_text() == 'active' else 3)
                    if action == 'stop': state.write_text('inactive')
                    elif action == 'start': state.write_text('active')
                    elif action == 'restart':
                        if args[-1].endswith('-volume.service'):
                            subprocess.run(['podman', 'volume', 'create', os.environ['QA_VOLUME']],
                                           check=True, stdout=subprocess.DEVNULL)
                    elif action == 'show': print('loaded')
                    elif action != 'daemon-reload': raise SystemExit('unexpected systemctl action')
                """)
                systemd_state = root / "systemd-state"
                systemd_state.write_text("active")
                command("podman", "volume", "create", volume)
                created = True
                mountpoint = Path(command("podman", "volume", "inspect", "--format",
                                          "{{.Mountpoint}}", volume).stdout.strip())
                (mountpoint / "payload.txt").write_text("original\n")
                (mountpoint / "config.json").write_text(json.dumps({
                    "admin_password": "synthetic-only-password",
                    "admin_session_generation": "before-restore"}) + "\n")
                env = dict(os.environ,
                           PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                           QL_STATE_ROOT=str(root / "state"), QA_PRODUCT=product,
                           QA_BACKUP_ROOT=str(root / "backups" / app),
                           QA_VOLUME=volume, QA_OLD_UNIT=str(old_unit),
                           QA_NEW_UNIT=str(new_unit), QA_STATE=str(state),
                           QA_SYSTEMD_STATE=str(systemd_state))
                outcome = subprocess.run(
                    ["bash", str(lifecycle), product, "upgrade", "--release-manifest",
                     str(root / "approved.json"), "--source-checkout", str(root / "canonical")],
                    env=env, text=True, capture_output=True, timeout=100,
                )
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn("upgrade rolled back", outcome.stderr)
                backups = list((root / "backups" / app).glob("*/" + volume + "-*.tar"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(subprocess.run(
                    ["sha256sum", "-c", "--quiet", backups[0].name + ".sha256"],
                    cwd=backups[0].parent, capture_output=True).returncode, 0)
                self.assertEqual(hashlib.sha256(old_unit.read_bytes()).hexdigest(), old_sha)
                self.assertFalse(new_unit.exists())
                self.assertEqual((state / "manifest").read_text(), original_manifest)
                restored_mount = Path(command("podman", "volume", "inspect", "--format",
                                              "{{.Mountpoint}}", volume).stdout.strip())
                self.assertEqual((restored_mount / "payload.txt").read_text(), "original\n")
                config = json.loads((restored_mount / "config.json").read_text())
                self.assertNotIn(config["admin_session_generation"], ("before-restore", "mutated"))
                self.assertEqual(systemd_state.read_text(), "active")
        finally:
            if created:
                command("podman", "volume", "rm", volume)

    def test_headscale_upgrade_restores_previous_unit_after_installer_failure(self):
        with tempfile.TemporaryDirectory(prefix="qa-headscale-release-rollback-") as directory:
            root = Path(directory)
            deploy = root / "products/deploy/headscale"
            upgrade = deploy / "scripts/upgrade.sh"
            executable(upgrade, (ROOT / "products/deploy/headscale/scripts/upgrade.sh").read_text())
            (deploy / "VERSION").write_text("synthetic-candidate\n")
            library = root / "scripts/lib/quadlet-lib.sh"
            library.parent.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts/lib/quadlet-lib.sh", library)
            with library.open("a") as stream:
                stream.write("\nql_require_rootless() { :; }\n"
                             "ql_require_user_systemd() { :; }\n"
                             "ql_wait_container_healthy() { :; }\n")
            executable(root / "products/deploy/release-gate.sh", """
                #!/usr/bin/env bash
                mcp_release_gate() { RELEASE_IMAGE_ID=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; }
            """)
            executable(deploy / "scripts/backup.sh", """
                #!/usr/bin/env bash
                touch "$QA_BACKUP_MARKER"
            """)
            executable(deploy / "scripts/install.sh", """
                #!/usr/bin/env bash
                [[ $(cat "$QA_SYSTEMD_STATE") == inactive ]] || exit 9
                printf 'Image=synthetic-new\n' > "$QA_OLD_UNIT"
                printf 'newhash  %s\n' "$QA_OLD_UNIT" > "$QA_STATE/manifest"
                exit 7
            """)
            executable(deploy / "scripts/smoke.sh", """
                #!/usr/bin/env bash
                touch "$QA_SMOKE_MARKER"
            """)
            executable(root / "bin/systemctl", """
                #!/usr/bin/env bash
                [[ $1 == --user ]] || exit 9
                case $2 in
                  is-active) [[ $(cat "$QA_SYSTEMD_STATE") == active ]];;
                  stop) printf 'inactive\n' > "$QA_SYSTEMD_STATE";;
                  restart) printf 'active\n' > "$QA_SYSTEMD_STATE";;
                  daemon-reload) :;;
                  *) exit 9;;
                esac
            """)
            executable(root / "bin/podman", """
                #!/usr/bin/env bash
                case "$1 $2" in
                  'container exists') exit 0;;
                  'container inspect')
                    if [[ $* == *State.Running* ]]; then
                      [[ $(cat "$QA_SYSTEMD_STATE") == active ]] && echo true || echo false
                    else
                      printf '%s\n' "$QA_STALE_CONTAINER_IMAGE_ID"
                    fi;;
                  'image exists') [[ $3 == "sha256:$QA_OLD_IMAGE_ID" ]];;
                  'image inspect') printf '%s\n' "$QA_OLD_IMAGE_ID";;
                  *) exit 9;;
                esac
            """)
            unit = root / "units/headscale-mcp-admin.container"
            unit.parent.mkdir()
            unit.write_text("Image=synthetic-old\n")
            original_sha = hashlib.sha256(unit.read_bytes()).hexdigest()
            old_image_id = "a" * 64
            pinned_unit = f"Image=sha256:{old_image_id}\n"
            pinned_sha = hashlib.sha256(pinned_unit.encode()).hexdigest()
            state = root / "state/headscale-mcp-admin"
            state.mkdir(parents=True)
            original_manifest = f"{original_sha}  {unit}\n"
            (state / "manifest").write_text(original_manifest)
            systemd_state = root / "systemd-state"
            env = dict(os.environ,
                       PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                       QL_STATE_ROOT=str(root / "state"),
                       QL_QUADLET_DIR=str(unit.parent),
                       QA_OLD_IMAGE_ID=old_image_id,
                       QA_OLD_UNIT=str(unit), QA_STATE=str(state),
                       QA_SYSTEMD_STATE=str(systemd_state),
                       QA_BACKUP_MARKER=str(root / "backup-called"),
                       QA_SMOKE_MARKER=str(root / "smoke-called"))
            for initial_state, stale_container_id in (
                ("active", old_image_id),
                ("inactive", "b" * 64),
            ):
                with self.subTest(initial_state=initial_state):
                    unit.write_text("Image=synthetic-old\n")
                    (state / "manifest").write_text(original_manifest)
                    systemd_state.write_text(initial_state + "\n")
                    env["QA_STALE_CONTAINER_IMAGE_ID"] = stale_container_id
                    outcome = subprocess.run(
                        ["bash", str(upgrade), "--release-manifest", str(root / "approved.json"),
                         "--source-checkout", str(root / "canonical")],
                        env=env, text=True, capture_output=True, timeout=20,
                    )
                    self.assertNotEqual(outcome.returncode, 0)
                    self.assertIn("rolled back", outcome.stderr)
                    self.assertTrue((root / "backup-called").exists())
                    self.assertTrue((root / "smoke-called").exists())
                    self.assertEqual(unit.read_text(), pinned_unit)
                    self.assertEqual((state / "manifest").read_text(), f"{pinned_sha}  {unit}\n")
                    self.assertEqual(systemd_state.read_text(), "active\n")
