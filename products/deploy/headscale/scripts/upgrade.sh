#!/usr/bin/env bash
# scripts/upgrade.sh: move headscale-mcp-admin to the version this checkout pins, with an
# automatic rollback when the new version fails its smoke test.
#
#   scripts/upgrade.sh --release-manifest REVIEWED.json --source-checkout CANONICAL
#
#   1. snapshot the installed unit files and the install manifest
#   2. scripts/backup.sh (volume export)          (skipped with --no-backup)
#   3. scripts/install.sh: verifies the reviewed local image ID, re-renders the
#      units and restarts what changed, then runs tests/smoke.sh
#   4. on any failure: put the snapshot back, restart, re-run tests/smoke.sh, exit 1
#
# The snapshot records the previous immutable image ID, so a retag cannot
# redirect rollback to the candidate image.
# SMOKE_FORCE_FAIL=1 makes step 3 fail on purpose to exercise the rollback.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
DEPLOY=$REPO/products/deploy/headscale
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=../../release-gate.sh
. "$REPO/products/deploy/release-gate.sh"
APP=headscale-mcp-admin
UNITS=(headscale-mcp-admin.service)
HEALTH_CONTAINER=headscale-mcp-admin
KEEP_SNAPSHOTS=5

backup=1 release_manifest='' source_checkout=''
while (($#)); do
  case $1 in
    --no-backup) backup=0 ;;
    --release-manifest) release_manifest=${2:?--release-manifest needs a file}; shift ;;
    --source-checkout) source_checkout=${2:?--source-checkout needs a directory}; shift ;;
    -h | --help) sed -n '2,15p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
ql_require_rootless
mcp_release_gate "$APP" headscale "$release_manifest" "$source_checkout"
ql_require_user_systemd
STATE=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/$APP
[[ -s $STATE/manifest ]] || ql_die "$APP is not installed (no $STATE/manifest); run scripts/install.sh"
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
installed_unit=$QDIR/$APP.container
[[ -f $installed_unit ]] || ql_die "installed Headscale Quadlet is missing: $installed_unit"

# Prefer the image actually running in the existing container. If the service
# is stopped, resolve its current unit reference before changing anything.
if podman container exists "$HEALTH_CONTAINER"; then
  old_image_id=$(podman container inspect "$HEALTH_CONTAINER" --format '{{.Image}}')
else
  old_image_ref=$(sed -n 's/^Image=//p' "$installed_unit")
  [[ -n $old_image_ref ]] || ql_die "installed Headscale Quadlet has no image"
  old_image_id=$(podman image inspect "$old_image_ref" --format '{{.Id}}')
fi
old_image_id=${old_image_id#sha256:}
[[ $old_image_id =~ ^[0-9a-f]{64}$ ]] || ql_die "cannot identify the previous Headscale image"
podman image exists "sha256:$old_image_id" || ql_die "previous Headscale image is unavailable for rollback"

# ---- 1. snapshot ------------------------------------------------------------------------------
snap=$STATE/rollback/$(date +%Y%m%d-%H%M%S)
(umask 077 && mkdir -p -- "$snap/files") || ql_die "cannot create $snap"
cp -p -- "$STATE/manifest" "$snap/manifest"
while read -r _ path; do
  [[ -f $path ]] || continue
  mkdir -p -- "$snap/files${path%/*}"
  cp -p -- "$path" "$snap/files$path"
done <"$STATE/manifest"
python3 - "$snap/files$installed_unit" "$snap/manifest" "$installed_unit" "$old_image_id" <<'PY'
import hashlib
from pathlib import Path
import sys

unit = Path(sys.argv[1])
manifest = Path(sys.argv[2])
installed, image_id = sys.argv[3:5]
lines = unit.read_bytes().splitlines(keepends=True)
images = [i for i, line in enumerate(lines) if line.startswith(b"Image=")]
if len(images) != 1:
    raise SystemExit("rollback Quadlet must contain exactly one image")
lines[images[0]] = f"Image=sha256:{image_id}\n".encode()
unit.write_bytes(b"".join(lines))
digest = hashlib.sha256(unit.read_bytes()).hexdigest()
entries = manifest.read_text().splitlines(keepends=True)
found = 0
for index, line in enumerate(entries):
    if line.partition("  ")[2].strip() == installed:
        entries[index] = f"{digest}  {installed}\n"
        found += 1
if found != 1:
    raise SystemExit("installed Quadlet is missing from the rollback manifest")
manifest.write_text("".join(entries))
PY
ql_info "snapshot of the installed units: $snap"
snaps=("$STATE"/rollback/*/) # timestamp names: glob order is age order
if ((${#snaps[@]} > KEEP_SNAPSHOTS)); then rm -rf -- "${snaps[@]:0:${#snaps[@]}-KEEP_SNAPSHOTS}"; fi

rollback() {
  local path rel
  ql_warn "upgrade failed: rolling back to the snapshot $snap"
  if [[ -f $STATE/manifest ]]; then
    while read -r _ path; do
      grep -qF -- "  $path" "$snap/manifest" || rm -f -- "$path"
    done <"$STATE/manifest"
  fi
  while IFS= read -r -d '' rel; do
    rel=${rel#"$snap/files"}
    mkdir -p -- "${rel%/*}"
    cp -p -- "$snap/files$rel" "$rel"
  done < <(find "$snap/files" -type f -print0)
  cp -p -- "$snap/manifest" "$STATE/manifest"
  rm -f -- "$STATE/pending-restart"
  systemctl --user daemon-reload
  systemctl --user restart "${UNITS[@]}" || ql_die "rollback: restart failed; see journalctl --user -u ${UNITS[0]} -n 100"
  ql_wait_container_healthy "$HEALTH_CONTAINER" 180 || ql_die "rollback: $HEALTH_CONTAINER is not healthy"
  if env -u SMOKE_FORCE_FAIL "$DEPLOY/scripts/smoke.sh"; then
    ql_warn "rolled back; the previous version is running again"
  else
    ql_warn "rolled back, but tests/smoke.sh still fails; restore data with scripts/restore.sh if needed"
  fi
  exit 1
}

# ---- 2. backup -------------------------------------------------------------------------------
if ((backup)); then
  "$DEPLOY/scripts/backup.sh" >/dev/null || ql_die "backup failed; nothing was changed"
fi

# ---- 3. install + smoke (install.sh runs tests/smoke.sh) ------------------------------------
# A direct install must refuse an active service on another image. An upgrade
# has already snapshotted and backed up that service, so stop it before handing
# control to the same fail-closed installer.
if systemctl --user is-active --quiet "${UNITS[0]}"; then
  systemctl --user stop "${UNITS[0]}" || rollback
fi
if ! "$DEPLOY/scripts/install.sh" --release-manifest "$release_manifest" \
    --source-checkout "$source_checkout"; then rollback; fi
ql_info "upgrade to $(<"$DEPLOY/VERSION") complete; rollback snapshot kept in $snap"
