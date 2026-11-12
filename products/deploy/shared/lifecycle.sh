#!/usr/bin/env bash
# Backup, restore, upgrade and uninstall for shared Quadlet MCP products.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
DEPLOY=$REPO/products/deploy
# shellcheck source=../../../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=../release-gate.sh
. "$DEPLOY/release-gate.sh"
PRODUCT=${1:?product required}
ACTION=${2:?action required}
shift 2
mapfile -d '' -t META < <(python3 "$DEPLOY/shared/catalog.py" get "$PRODUCT")
((${#META[@]} == 5)) || ql_die "unknown product: $PRODUCT"
[[ ${META[4]} == shared ]] || ql_die "use the dedicated $PRODUCT lifecycle script"
APP=$PRODUCT-mcp-admin
UNIT=$APP.service
VOLUME=${APP//-/_}_data
VOLUME_UNIT=$APP-volume.service
ENV_FILE=$HOME/.config/$APP/$APP.env
STATE=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/$APP
export QL_APP=$APP

ql_require_rootless
if [[ $ACTION != upgrade ]]; then
  ql_require_user_systemd
  ql_lock "$APP"
fi

backup() {
  local dest=${1:-$HOME/backups/$APP} out base n=2
  podman volume exists "$VOLUME" || ql_die "volume $VOLUME does not exist"
  base=$dest/$(date +%Y%m%d-%H%M%S)
  out=$base
  while [[ -e $out ]]; do out=$base-$n; n=$((n + 1)); done
  (umask 077 && mkdir -p -- "$out") || ql_die "cannot create $out"
  chmod 700 -- "$out"
  stopped=0
  resume() {
    ((stopped)) || return 0
    stopped=0
    systemctl --user start "$UNIT" || ql_warn "could not restart $UNIT after backup"
  }
  ql_cleanup resume resume
  if systemctl --user is-active --quiet "$UNIT"; then
    stopped=1
    systemctl --user stop "$UNIT" || ql_die "could not stop $UNIT for a consistent backup"
  fi
  ql_backup_volume "$VOLUME" "$out" >/dev/null
  resume
  if [[ -f $ENV_FILE ]]; then
    ql_env_load "$ENV_FILE"
    ql_wait_http "http://$(ql_env_get MCP_ADMIN_BIND):$(ql_env_get MCP_ADMIN_PORT)/healthz" 200 60 \
      || ql_die "$UNIT did not answer after backup; backup remains $out"
  fi
  ql_cleanup_clear resume
  if [[ -f $ENV_FILE ]]; then install -m 600 -- "$ENV_FILE" "$out/${ENV_FILE##*/}"; fi
  ql_info "backup complete: $out"
  printf '%s\n' "$out"
}

restore() {
  local dir=$1 answer tarfile mountpoint
  shift
  [[ -d $dir ]] || ql_die "backup directory $dir does not exist"
  [[ ${1:-} == --yes ]] || {
    [[ -t 0 ]] || ql_die "restore replaces $VOLUME; add --yes for a noninteractive run"
    read -r -p "Type '$APP' to replace $VOLUME: " answer
    [[ $answer == "$APP" ]] || ql_die "restore cancelled"
  }
  shopt -s nullglob
  local -a tars=("$dir/$VOLUME"-*.tar)
  ((${#tars[@]} == 1)) || ql_die "expected exactly one $VOLUME-*.tar in $dir"
  tarfile=${tars[0]}
  [[ -f $tarfile.sha256 ]] || ql_die "backup checksum missing"
  (cd -- "$dir" && sha256sum -c --quiet -- "${tarfile##*/}.sha256") || ql_die "backup checksum failed"
  [[ $(systemctl --user show -p LoadState --value "$UNIT") == loaded ]] || ql_die "$UNIT not installed"
  systemctl --user stop "$UNIT" || ql_die "could not stop $UNIT"
  if podman volume exists "$VOLUME"; then
    podman volume rm "$VOLUME" >/dev/null || ql_die "could not remove $VOLUME"
  fi
  systemctl --user restart "$VOLUME_UNIT" || ql_die "could not recreate $VOLUME"
  podman volume import "$VOLUME" "$tarfile" || ql_die "volume import failed; service remains stopped"
  mountpoint=$(podman volume inspect --format '{{.Mountpoint}}' "$VOLUME")
  podman unshare python3 "$DEPLOY/shared/rotate-restored-auth.py" "$mountpoint/config.json" \
    || ql_die "could not invalidate restored GUI sessions; service remains stopped"
  systemctl --user start "$UNIT" || ql_die "could not start restored $UNIT"
  ql_wait_container_healthy "$APP" 180 || ql_die "restored container not healthy"
  "$DEPLOY/manage.sh" "$PRODUCT" smoke || ql_die "restored service failed smoke"
  ql_info "restore complete: $dir"
}

case $ACTION in
  backup)
    if [[ ${1:-} == --dest ]]; then backup "${2:?--dest needs a directory}"; else backup; fi
    ;;
  restore)
    ((${#} >= 1)) || ql_die "usage: manage.sh $PRODUCT restore BACKUP_DIR [--yes]"
    restore "$@"
    ;;
  upgrade)
    release_manifest='' source_checkout=''
    while (($#)); do
      case $1 in
        --release-manifest) release_manifest=${2:?--release-manifest needs a file}; shift ;;
        --source-checkout) source_checkout=${2:?--source-checkout needs a directory}; shift ;;
        *) ql_die "unknown upgrade option $1" ;;
      esac
      shift
    done
    mcp_release_gate "$APP" "$PRODUCT" "$release_manifest" "$source_checkout"
    ql_require_user_systemd
    [[ -s $STATE/manifest ]] || ql_die "$APP is not installed"
    snapshot=$STATE/rollback/$(date +%Y%m%d-%H%M%S)
    (umask 077 && mkdir -p -- "$snapshot/files")
    cp -p -- "$STATE/manifest" "$snapshot/manifest"
    while read -r _ path; do
      [[ -f $path ]] || continue
      mkdir -p -- "$snapshot/files${path%/*}"
      cp -p -- "$path" "$snapshot/files$path"
    done <"$STATE/manifest"
    backup_dir=$(backup) || ql_die "backup failed; upgrade not attempted"
    if ! "$DEPLOY/manage.sh" "$PRODUCT" install --release-manifest "$release_manifest" \
        --source-checkout "$source_checkout"; then
      ql_warn "upgrade failed; restoring previous units and volume"
      if [[ -f $STATE/manifest ]]; then
        while read -r _ path; do
          grep -qF -- "  $path" "$snapshot/manifest" || rm -f -- "$path"
        done <"$STATE/manifest"
      fi
      while IFS= read -r -d '' path; do
        rel=${path#"$snapshot/files"}
        mkdir -p -- "${rel%/*}"
        cp -p -- "$path" "$rel"
      done < <(find "$snapshot/files" -type f -print0)
      cp -p -- "$snapshot/manifest" "$STATE/manifest"
      rm -f -- "$STATE/pending-restart"
      systemctl --user daemon-reload || ql_die "rollback reload failed"
      restore "$backup_dir" --yes || ql_die "rollback restore failed; backup remains $backup_dir"
      ql_die "upgrade rolled back to previous version"
    fi
    ql_info "upgrade complete; rollback snapshot $snapshot"
    ;;
  uninstall)
    purge=0 yes=0
    while (($#)); do
      case $1 in
        --purge) purge=1 ;;
        --yes) yes=1 ;;
        --dry-run) export QL_DRY_RUN=1 ;;
        *) ql_die "unknown uninstall option $1" ;;
      esac
      shift
    done
    if ((purge)) && [[ ${QL_DRY_RUN:-0} != 1 ]]; then
      if ((!yes)); then
        [[ -t 0 ]] || ql_die "--purge deletes data; add --yes for a noninteractive run"
        read -r -p "Type '$APP' to delete its volume and secrets: " answer
        [[ $answer == "$APP" ]] || ql_die "uninstall cancelled"
      fi
      backup >/dev/null
    fi
    if ((purge)); then ql_uninstall_units "$APP" --purge; else ql_uninstall_units "$APP"; fi
    ;;
  *) ql_die "unknown lifecycle action $ACTION" ;;
esac
