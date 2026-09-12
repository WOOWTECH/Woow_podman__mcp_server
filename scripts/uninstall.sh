#!/usr/bin/env bash
# scripts/uninstall.sh: remove the podman-mcp-admin Quadlet units. Keeps data by default.
#
#   scripts/uninstall.sh                   stop + remove the units; keep the podman_mcp_data
#                                          volume, the network, the secrets, the image and the
#                                          env file (a re-install adopts them unchanged)
#   scripts/uninstall.sh --purge [--yes]   also delete the volume (after a final backup to
#                                          ~/backups/podman-mcp-admin/), the network and the
#                                          secrets. The ONLY way this repo deletes data.
#   scripts/uninstall.sh --purge-images    also remove the localhost/woow-podman-mcp-admin:* images
#   scripts/uninstall.sh --dry-run         report what would be removed
#
# Never touched: podman.socket (other stacks use it), the env file in
# ~/.config/podman-mcp-admin/ (delete it yourself after --purge).
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"

# ---- per-repo settings -----------------------------------------------------------------
APP=podman-mcp-admin
DATA_VOLUMES=(podman_mcp_data)                  # exported before --purge deletes them
UNIT=podman-mcp-admin.service
BACKUP_DIR=$HOME/backups/$APP
IMAGE_REPO=localhost/woow-podman-mcp-admin
# ------------------------------------------------------------------------------------------

purge=0 yes=0 purge_images=0
while (($#)); do
  case $1 in
    --purge) purge=1 ;;
    --purge-images) purge_images=1 ;;
    --yes) yes=1 ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    -h | --help) sed -n '2,15p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
DRY=${QL_DRY_RUN:-0}
ql_require_rootless
ql_lock "$APP"

if ((purge)); then
  if ((!yes)) && [[ $DRY != 1 ]]; then
    [[ -t 0 ]] || ql_die "--purge deletes the volume, network and secrets; add --yes to confirm non-interactively"
    read -r -p "Type '$APP' to delete its volume (admin password, connector token), network and secrets: " answer
    [[ $answer == "$APP" ]] || ql_die "aborted; nothing was deleted"
  fi
  if [[ $DRY != 1 ]]; then
    # The volumes are deleted a few lines below, so there is nothing to keep running: stop
    # the writer first and the export is consistent instead of "may be inconsistent".
    systemctl --user stop "$UNIT" 2>/dev/null \
      || ql_warn "could not stop $UNIT; the final export may be inconsistent"
    for v in "${DATA_VOLUMES[@]}"; do
      if podman volume exists "$v"; then ql_backup_volume "$v" "$BACKUP_DIR" >/dev/null; fi
    done
  fi
  ql_uninstall_units "$APP" --purge
else
  ql_uninstall_units "$APP"
fi

if ((purge_images)); then
  mapfile -t imgs < <(podman images --format '{{.Repository}}:{{.Tag}}' | grep -E "^${IMAGE_REPO}:" || true)
  for img in "${imgs[@]}"; do
    if [[ $DRY == 1 ]]; then ql_info "[dry-run] would remove image $img"; continue; fi
    if podman rmi "$img" >/dev/null 2>&1; then ql_info "removed image $img"; else ql_warn "could not remove image $img (in use?)"; fi
  done
fi
