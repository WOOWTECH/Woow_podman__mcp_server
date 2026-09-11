#!/usr/bin/env bash
# scripts/upgrade.sh: move podman-mcp-admin to the version this checkout pins, with an
# automatic rollback when the new version fails its smoke test.
#
#   git pull && scripts/upgrade.sh [--no-backup]
#
#   1. snapshot the installed unit files and the install manifest
#   2. scripts/backup.sh (volume export)          (skipped with --no-backup)
#   3. scripts/install.sh: builds localhost/woow-podman-mcp-admin:$(cat VERSION) if missing,
#      re-renders the units and restarts what changed, then runs tests/smoke.sh
#   4. on any failure: put the snapshot back, restart, re-run tests/smoke.sh, exit 1
#
# The previous image tag is never deleted, so the restored units start the old version.
# SMOKE_FORCE_FAIL=1 makes step 3 fail on purpose to exercise the rollback.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=podman-mcp-admin
UNITS=(podman-mcp-admin.service)
HEALTH_CONTAINER=podman-mcp-admin
KEEP_SNAPSHOTS=5

backup=1
while (($#)); do
  case $1 in
    --no-backup) backup=0 ;;
    -h | --help) sed -n '2,15p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd
STATE=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/$APP
[[ -s $STATE/manifest ]] || ql_die "$APP is not installed (no $STATE/manifest); run scripts/install.sh"

# ---- 1. snapshot ------------------------------------------------------------------------------
snap=$STATE/rollback/$(date +%Y%m%d-%H%M%S)
(umask 077 && mkdir -p -- "$snap/files") || ql_die "cannot create $snap"
cp -p -- "$STATE/manifest" "$snap/manifest"
while read -r _ path; do
  [[ -f $path ]] || continue
  mkdir -p -- "$snap/files${path%/*}"
  cp -p -- "$path" "$snap/files$path"
done <"$STATE/manifest"
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
  if env -u SMOKE_FORCE_FAIL "$REPO/tests/smoke.sh"; then
    ql_warn "rolled back; the previous version is running again"
  else
    ql_warn "rolled back, but tests/smoke.sh still fails; restore data with scripts/restore.sh if needed"
  fi
  exit 1
}

# ---- 2. backup -------------------------------------------------------------------------------
if ((backup)); then
  "$REPO/scripts/backup.sh" >/dev/null || ql_die "backup failed; nothing was changed"
fi

# ---- 3. install + smoke (install.sh runs tests/smoke.sh) ------------------------------------
if ! "$REPO/scripts/install.sh"; then rollback; fi
ql_info "upgrade to $(<"$REPO/VERSION") complete; rollback snapshot kept in $snap"
