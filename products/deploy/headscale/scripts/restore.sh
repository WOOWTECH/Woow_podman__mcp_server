#!/usr/bin/env bash
# scripts/restore.sh: restore a scripts/backup.sh directory into the installed units.
#
#   scripts/restore.sh <backup_dir> [--with-secrets] [--yes]
#
#   --with-secrets   also replace the podman secrets from <backup_dir>/secrets.env
#   --yes            do not ask for confirmation
#
# Stops headscale-mcp-admin and REPLACES the headscale_mcp_data volume with the shared core's
# fully revoked copy of the backup (restore_guard, run in the installed image): profiles,
# connections and tool switches come back, but every MCP token is revoked, the GUI password
# becomes the headscale-mcp-admin-password secret and no GUI session survives. Then it starts
# the service and runs smoke.sh; issue new MCP tokens in the GUI afterwards.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
DEPLOY=$REPO/products/deploy/headscale
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=headscale-mcp-admin
UNIT=headscale-mcp-admin.service
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
VOLUME=headscale_mcp_data
VOLUME_UNIT=headscale-mcp-volume.service
SECRET_VARS=(headscale-mcp-admin-jwt-secret:JWT_SECRET headscale-mcp-admin-password:ADMIN_PASSWORD)

dir='' with_secrets=0 yes=0
while (($#)); do
  case $1 in
    --with-secrets) with_secrets=1 ;;
    --yes) yes=1 ;;
    -h | --help) sed -n '2,12p' "$0"; exit 0 ;;
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
image=$(sed -n 's/^Image=//p' "$QDIR/$APP.container" 2>/dev/null || true)
[[ $image == sha256:* ]] && podman image exists "$image" \
  || ql_die "the installed Headscale image is not available; nothing was changed"
# shellcheck source=../../shared/restore-volume.sh
. "$REPO/products/deploy/shared/restore-volume.sh"

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

# Secrets first: the restored GUI password is whatever headscale-mcp-admin-password holds.
if ((with_secrets)); then
  ql_env_load "$dir/secrets.env"
  for sv in "${SECRET_VARS[@]}"; do
    if [[ -n ${QL_ENV[${sv#*:}]+x} ]]; then ql_secret_ensure "${sv%%:*}" "env:${sv#*:}" --update; fi
  done
fi
# restore_guard never restores admin-sessions.sqlite3: the service starts a new session database.
mcp_restore_revoked "$VOLUME" "$tar" "$image" headscale-mcp-admin-password headscale \
  || ql_die "restore preparation failed; $VOLUME is empty and the service was not restarted"
ql_info "restored $VOLUME from ${tar##*/} with every MCP token revoked"

systemctl --user start "$UNIT"
ql_wait_container_healthy headscale-mcp-admin 180 || ql_die "headscale-mcp-admin did not become healthy after the restore"
"$DEPLOY/scripts/smoke.sh" || ql_die "Headscale smoke check failed after the restore"
ql_info "restore complete: sign in with the password in podman secret headscale-mcp-admin-password and issue new MCP tokens"
