#!/usr/bin/env bash
# scripts/backup.sh: back up headscale-mcp-admin into a new directory.
#
#   scripts/backup.sh [--dest DIR] [--include-secrets]
#
#   --dest DIR          parent directory (default ~/backups/headscale-mcp-admin); a <timestamp>/
#                       subdirectory is created in it and printed on stdout
#   --include-secrets   also write the two podman secrets to secrets.env (0600). Without it
#                       a restore keeps the target host's secrets, which is fine: config.json
#                       in the volume holds the live admin password and connector token.
#   The container is stopped during export for consistent config and SQLite state.
#
# Contents: headscale_mcp_data-<ts>.tar (volume export: /data/config.json with the admin password
# and the connector token; treat it as a credential) + .sha256, and a copy of the env file.
# Every file is 0600 in a 0700 directory. Restore with scripts/restore.sh <dir>.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
DEPLOY=$REPO/products/deploy/headscale
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=headscale-mcp-admin
ENV_FILE=$HOME/.config/$APP/$APP.env
VOLUME=headscale_mcp_data
SECRET_VARS=(headscale-mcp-admin-jwt-secret:JWT_SECRET headscale-mcp-admin-password:ADMIN_PASSWORD)

UNIT=headscale-mcp-admin.service

dest=$HOME/backups/$APP include_secrets=0 stop=1
while (($#)); do
  case $1 in
    --dest) dest=${2:?--dest needs a directory}; shift ;;
    --include-secrets) include_secrets=1 ;;
    --stop) stop=1 ;; # accepted for compatibility; stopping is the default
    -h | --help) sed -n '2,19p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
ql_require_rootless
podman volume exists "$VOLUME" || ql_die "volume $VOLUME does not exist; nothing to back up"

base=$dest/$(date +%Y%m%d-%H%M%S) out=$dest/$(date +%Y%m%d-%H%M%S) n=2
while [[ -e $out ]]; do out=$base-$n; n=$((n + 1)); done
(umask 077 && mkdir -p -- "$out") || ql_die "cannot create $out"
chmod 700 "$out"

# Stop the writer for a consistent export of config.json and the session SQLite database.
stopped=0
start_stopped() { # idempotent: called from the EXIT trap and again on the happy path
  ((stopped)) || return 0
  stopped=0
  systemctl --user start "$UNIT" \
    || ql_warn "could not start $UNIT again; run: systemctl --user start $UNIT"
}
if ((stop)) && systemctl --user is-active --quiet "$UNIT" 2>/dev/null; then
  trap start_stopped EXIT
  stopped=1
  ql_info "stopping $UNIT for a consistent volume export"
  systemctl --user stop "$UNIT" || ql_die "could not stop $UNIT"
fi

ql_backup_volume "$VOLUME" "$out" >/dev/null

start_stopped
trap - EXIT
if [[ -f $ENV_FILE ]]; then install -m 600 -- "$ENV_FILE" "$out/${ENV_FILE##*/}"; fi
if ((include_secrets)); then
  (
    umask 077
    for sv in "${SECRET_VARS[@]}"; do
      name=${sv%%:*} var=${sv#*:}
      if value=$(podman secret inspect --showsecret --format '{{.SecretData}}' "$name" 2>/dev/null); then
        printf '%s=%s\n' "$var" "$value"
      else
        ql_warn "secret $name not found; not in secrets.env"
      fi
    done >"$out/secrets.env"
  )
  ql_warn "$out/secrets.env holds the secrets in plain text (0600): keep the backup private"
fi
ql_info "backup complete: $out"
printf '%s\n' "$out"
