#!/usr/bin/env bash
# scripts/restore.sh: restore a scripts/backup.sh directory into the installed units.
#
#   scripts/restore.sh <backup_dir> [--with-secrets] [--yes]
#
#   --with-secrets   also replace the podman secrets from <backup_dir>/secrets.env
#   --yes            do not ask for confirmation
#
# Stops headscale-mcp-admin, REPLACES the headscale_mcp_data volume with the backup's export
# (the current admin password, connector token and tool switches are overwritten), then
# invalidates restored GUI sessions, then starts it and runs smoke.sh.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
DEPLOY=$REPO/products/deploy/headscale
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=headscale-mcp-admin
UNIT=headscale-mcp-admin.service
VOLUME=headscale_mcp_data
VOLUME_UNIT=headscale-mcp-volume.service
SECRET_VARS=(headscale-mcp-admin-jwt-secret:JWT_SECRET headscale-mcp-admin-password:ADMIN_PASSWORD)

dir='' with_secrets=0 yes=0
while (($#)); do
  case $1 in
    --with-secrets) with_secrets=1 ;;
    --yes) yes=1 ;;
    -h | --help) sed -n '2,11p' "$0"; exit 0 ;;
    -*) ql_die "unknown option $1 (see --help)" ;;
    *) [[ -z $dir ]] || ql_die "one backup directory only"; dir=$1 ;;
  esac
  shift
done
[[ -n $dir && -d $dir ]] || ql_die "usage: scripts/restore.sh <backup_dir> [--with-secrets] [--yes]"
export QL_APP=$APP
ql_require_rootless
ql_require_user_systemd
ql_lock "$APP"

shopt -s nullglob
tars=("$dir/$VOLUME"-*.tar)
((${#tars[@]} == 1)) || ql_die "expected exactly one $VOLUME-*.tar in $dir, found ${#tars[@]}"
tar=${tars[0]}
if [[ -f $tar.sha256 ]]; then
  (cd -- "$dir" && sha256sum -c --quiet -- "${tar##*/}.sha256") || ql_die "checksum mismatch for $tar"
else
  ql_warn "no $tar.sha256; restoring without a checksum"
fi
[[ $(systemctl --user show -p LoadState --value "$UNIT" 2>/dev/null) == loaded ]] \
  || ql_die "$UNIT is not installed; run scripts/install.sh first"
((with_secrets == 0)) || [[ -f $dir/secrets.env ]] || ql_die "--with-secrets: $dir/secrets.env not found"

if ((!yes)); then
  [[ -t 0 ]] || ql_die "restore replaces the $VOLUME volume; add --yes to confirm non-interactively"
  read -r -p "Replace volume $VOLUME with ${tar##*/}? Type '$APP' to continue: " answer
  [[ $answer == "$APP" ]] || ql_die "aborted; nothing was changed"
fi

ql_info "stopping $UNIT"
systemctl --user stop "$UNIT"
if podman volume exists "$VOLUME"; then
  podman volume rm "$VOLUME" >/dev/null || ql_die "cannot remove volume $VOLUME (still in use?)"
fi
systemctl --user restart "$VOLUME_UNIT" || ql_die "cannot recreate $VOLUME through $VOLUME_UNIT"
podman volume import "$VOLUME" "$tar" || ql_die "podman volume import failed; $VOLUME is empty now, re-run restore"
ql_info "restored $VOLUME from ${tar##*/}"

# An older backup can contain a session database from before a user's logout.
# Give the restored store a new epoch so all previously issued GUI cookies fail.
mountpoint=$(podman volume inspect --format '{{.Mountpoint}}' "$VOLUME")
podman unshare python3 "$DEPLOY/scripts/rotate-restored-sessions.py" \
  "$mountpoint/admin-sessions.sqlite3" \
  || ql_die "could not invalidate restored GUI sessions; service was not restarted"

if ((with_secrets)); then
  ql_env_load "$dir/secrets.env"
  for sv in "${SECRET_VARS[@]}"; do
    if [[ -n ${QL_ENV[${sv#*:}]+x} ]]; then ql_secret_ensure "${sv%%:*}" "env:${sv#*:}" --update; fi
  done
fi

systemctl --user start "$UNIT"
ql_wait_container_healthy headscale-mcp-admin 180 || ql_die "headscale-mcp-admin did not become healthy after the restore"
"$DEPLOY/scripts/smoke.sh" || ql_die "Headscale smoke check failed after the restore"
ql_info "restore complete"
