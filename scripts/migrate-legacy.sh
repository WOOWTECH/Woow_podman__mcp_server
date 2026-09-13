#!/usr/bin/env bash
# scripts/migrate-legacy.sh: move a hand-made podman-mcp-admin deployment (a `podman run`
# container plus a hand-written podman-podman-mcp-admin.service, or a `podman generate systemd`
# unit) to the Quadlet units of this repo. The volume podman_mcp_data is adopted where it is:
# no data is copied, the connector token and the admin password in /data/config.json stay, and
# the legacy container is kept for --rollback.
#
#   scripts/migrate-legacy.sh [--bind ADDR] [--port N] [--keep-exposure] [--suffix YYYYMMDD]
#                             [--api-version vX.Y.Z] [--prepare-only | --dry-run]
#                             [--no-auto-rollback] [--yes]
#   scripts/migrate-legacy.sh --rollback [--yes]
#   scripts/migrate-legacy.sh --status
#
#   --bind ADDR       publish address of the new unit. Default: 127.0.0.1, EVEN WHEN the legacy
#                     container published 0.0.0.0 - this container holds the podman socket, so
#                     the migration narrows the exposure deliberately and says so. Pass the
#                     legacy address explicitly, or --keep-exposure, to keep it.
#   --keep-exposure   keep the legacy publish address, whatever it was (prints a warning)
#   --port N          publish port (default: the port the legacy container published)
#   --api-version V   PODMAN_API_VERSION for the new unit (default: the legacy container's,
#                     or the daemon's own when the legacy one is newer than the daemon)
#   --suffix S        rename suffix for the legacy container (rename path only)
#   --prepare-only    steps 1-2 only: env file, secrets, image, backup, capture. No downtime.
#   --dry-run         step 1 plus a render of the units; changes nothing
#   --no-auto-rollback  leave a failed cutover in place for inspection
#   --rollback        undo the cutover: remove the Quadlet units, bring the legacy container
#                     and its unit back, and wait until it serves again
#   --status          print the recorded migration state
#
# Rollback shape (STANDARD 7a): the legacy container is kept either by renaming it and leaving
# it stopped, or - where the user unit podman-restart.service is enabled AND the container's
# restart policy is exactly `always`, because a renamed copy would then revive at the next boot
# and fight the Quadlet container for the name, the port and the socket - by capturing it into
# the backup directory and removing it. ql_rollback_strategy decides from this host's real
# state, never from its name. woowtechopenclaw's container is `unless-stopped`, so it takes the
# rename path there today; the capture path is supported and tested all the same, because that
# is a property of the host, not of this repo.
#
# Steps:  1 pre-flight (read-only): container, unit, volume, port, exposure, API version
#         2 prepare (no downtime): env file, adopted secrets, image build, backup + checksums,
#           volume fingerprint, and the capture when the host needs one
#         3 cutover: stop and disable the legacy unit, retire the container, install.sh
#         4 verify: healthy, HTTP, the volume is the SAME volume, smoke; report the downtime
#         5 --rollback
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=app.sh
. "$REPO/scripts/app.sh"

STATE=$APP_STATE_DIR/migration.state
LEGACY_ALL=("$APP_CONTAINER")
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
PLAIN_UNIT_DIR=${QL_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}
VERSION=$(<"$REPO/VERSION")
IMAGE=localhost/woow-podman-mcp-admin:$VERSION

mode=migrate bind='' port='' keep_exposure=0 api_version='' suffix=$(date +%Y%m%d)
auto_rollback=1 ASSUME_YES=0
while (($#)); do
  case $1 in
    --bind) bind=${2:?--bind needs an address}; shift ;;
    --keep-exposure) keep_exposure=1 ;;
    --port) port=${2:?--port needs a number}; shift ;;
    --api-version) api_version=${2:?--api-version needs vX.Y.Z}; shift ;;
    --suffix) suffix=${2:?--suffix needs a value}; shift ;;
    --prepare-only) mode=prepare ;;
    --dry-run) mode=dry-run ;;
    --no-auto-rollback) auto_rollback=0 ;;
    --rollback) mode=rollback ;;
    --status) mode=status ;;
    --yes) ASSUME_YES=1 ;;
    -h | --help) sed -n '2,44p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
ql_assert_match --suffix "$suffix" '[A-Za-z0-9._-]+'
[[ -z $bind || $keep_exposure == 0 ]] || ql_die "--bind and --keep-exposure contradict each other"

state_get() { if [[ -f $STATE ]]; then sed -n "s/^$1=//p" "$STATE" | tail -n1; fi; }
state_set() {
  local tmp
  mkdir -p "$APP_STATE_DIR"
  tmp=$(mktemp "$APP_STATE_DIR/.migration.XXXXXX")
  { if [[ -f $STATE ]]; then grep -v "^$1=" "$STATE" || true; fi; printf '%s=%s\n' "$1" "$2"; } >"$tmp"
  mv -f "$tmp" "$STATE"
}
confirm() {
  ((!ASSUME_YES)) || return 0
  [[ -t 0 ]] || ql_die "$1 (not a terminal: pass --yes)"
  local a
  read -r -p "$1. Continue? [y/N] " a
  [[ $a == [yY]* ]] || ql_die "aborted"
}
unit_exists() { [[ -n $(systemctl --user show -p FragmentPath --value "$1" 2>/dev/null) ]]; }
# now_ms: milliseconds since the epoch, for the downtime report
now_ms() { date +%s%3N; }

if [[ $mode == status ]]; then
  if [[ -f $STATE ]]; then cat "$STATE"; else echo "no migration recorded in $STATE"; fi
  exit 0
fi

ql_preflight "$PODMAN_MIN"
ql_lock "$APP"
export WOOW_QL_LOCK_HELD=$APP

# =============================================================================================
# 5. rollback
# =============================================================================================
rollback() {
  local status sfx bk saved u
  status=$(state_get STATUS) sfx=$(state_get SUFFIX) bk=$(state_get BACKUP)
  [[ $status == cutover || $status == "done" ]] || ql_die "nothing to roll back (migration status: ${status:-none})"
  confirm "--rollback removes the podman-mcp-admin Quadlet units and brings the legacy container back"
  local t0 t1
  t0=$(now_ms)
  if [[ -f $APP_STATE_DIR/manifest ]]; then
    ql_info "stopping and removing the Quadlet units (the volume $APP_VOLUME is kept)"
    ql_uninstall_units "$APP"
  else
    ql_info "no Quadlet manifest to remove (install.sh did not get that far)"
  fi
  # A Quadlet container is --rm/--replace, so it is gone with its unit; if anything is left
  # under our name it must go before the legacy container can take the name back.
  if podman container exists "$APP_CONTAINER"; then
    [[ $(podman inspect --format '{{index .Config.Labels "PODMAN_SYSTEMD_UNIT"}}' "$APP_CONTAINER") == "$APP_UNIT" ]] \
      || ql_die "a container named $APP_CONTAINER exists and is not our Quadlet leftover; resolve it by hand"
    podman rm -f "$APP_CONTAINER" >/dev/null
  fi
  app_legacy_restore "$sfx" "$bk" "${LEGACY_ALL[@]}"
  # The shadowing unit, if the cutover moved one aside, goes back before the unit is started.
  saved=$(state_get SHADOW_SAVED)
  if [[ -n $saved && -f $saved ]]; then
    install -m 644 -- "$saved" "$PLAIN_UNIT_DIR/$SHADOWING_UNIT" || ql_warn "could not restore $SHADOWING_UNIT"
    ql_info "restored $PLAIN_UNIT_DIR/$SHADOWING_UNIT from $saved"
  fi
  systemctl --user daemon-reload
  u=$(state_get LEGACY_UNIT)
  if [[ -n $u ]] && unit_exists "$u"; then
    [[ $(state_get LEGACY_UNIT_STATE) != enabled ]] || systemctl --user enable "$u" >/dev/null 2>&1 || true
    systemctl --user start "$u" || ql_warn "could not start $u; starting the container directly"
  fi
  app_running "$APP_CONTAINER" || podman start "$APP_CONTAINER" >/dev/null
  ql_wait_http "http://$(state_get LEGACY_HOST):$(state_get LEGACY_PORT)/healthz" '200' 180 \
    || ql_die "the legacy console did not answer /healthz after the rollback"
  t1=$(now_ms)
  state_set STATUS rolled-back
  ql_info "rolled back in $(((t1 - t0) / 1000)).$(printf '%03d' $(((t1 - t0) % 1000))) s: the legacy deployment serves again. Backup of the attempt: $bk"
}

if [[ $mode == rollback ]]; then
  rollback
  exit 0
fi

# =============================================================================================
# 1. pre-flight checks (read-only). Every one of them refuses rather than guesses.
# =============================================================================================
ql_info "step 1/4: pre-flight checks"
case $(state_get STATUS) in
  cutover | "done") ql_die "a cutover is already recorded in $STATE (use --status, or --rollback)" ;;
esac

podman container exists "$APP_CONTAINER" \
  || ql_die "no container named $APP_CONTAINER on this host: there is nothing to migrate (a fresh host just runs scripts/install.sh)"
legacy_label=$(podman inspect --format '{{index .Config.Labels "PODMAN_SYSTEMD_UNIT"}}' "$APP_CONTAINER" 2>/dev/null || true)
[[ $legacy_label != "$APP_UNIT" ]] \
  || ql_die "$APP_CONTAINER is already managed by the Quadlet unit $APP_UNIT; this host needs no migration"
[[ -z $legacy_label || $legacy_label == '<no value>' ]] \
  || ql_die "$APP_CONTAINER belongs to the foreign unit $legacy_label; resolve that before migrating"
app_running "$APP_CONTAINER" || ql_die "$APP_CONTAINER is not running; start the legacy deployment first, so the migration can read its settings and prove the data is adopted"

# Quadlet units already installed -> this is not a migration.
if [[ -f $APP_STATE_DIR/manifest && $(state_get STATUS) != prepared ]]; then
  ql_die "the podman-mcp-admin Quadlet units are already installed (see $APP_STATE_DIR/manifest); this host needs no migration"
fi

# Data where it is expected: the volume the legacy container mounts at /data must be the named
# volume the .volume unit adopts, and it must exist.
legacy_vol=$(podman inspect --format \
  '{{range .Mounts}}{{if eq .Destination "'"$APP_DATA_DEST"'"}}{{.Type}}|{{.Name}}|{{.Source}}{{end}}{{end}}' "$APP_CONTAINER")
[[ $legacy_vol == volume\|* ]] \
  || ql_die "$APP_CONTAINER does not mount a named volume at $APP_DATA_DEST (got '${legacy_vol:-nothing}'); this migration adopts the volume $APP_VOLUME and cannot adopt a bind mount"
legacy_vol_name=${legacy_vol#volume|}
legacy_vol_name=${legacy_vol_name%%|*}
[[ $legacy_vol_name == "$APP_VOLUME" ]] \
  || ql_die "$APP_CONTAINER mounts the volume '$legacy_vol_name' at $APP_DATA_DEST, but quadlet/podman-mcp.volume declares VolumeName=$APP_VOLUME; rename the volume or adjust the unit before migrating"
podman volume exists "$APP_VOLUME" || ql_die "volume $APP_VOLUME does not exist"
VOL_ID=$(app_volume_identity "$APP_VOLUME") || ql_die "cannot read the identity of volume $APP_VOLUME"

# The socket. The Quadlet unit mounts the socket DIRECTORY (%t/podman) instead of the socket
# FILE the legacy container mounts, because a podman.socket restart replaces the file and a
# file bind would keep the dead inode. The capability handed over is the same one: full podman
# API access for this user. Anything else in the legacy mount list is a fact the new unit does
# not reproduce, so it is reported rather than silently dropped.
legacy_sock=$(app_mount_source "$APP_CONTAINER" "$APP_SOCKET_DEST")
[[ -n $legacy_sock ]] \
  || ql_die "$APP_CONTAINER has no mount at $APP_SOCKET_DEST: this is not the deployment this script migrates"
[[ -S $legacy_sock ]] || ql_warn "$legacy_sock is not a socket right now; the new unit mounts ${XDG_RUNTIME_DIR:-%t}/podman instead"
extra_mounts=$(podman inspect --format '{{range .Mounts}}{{.Destination}}{{println}}{{end}}' "$APP_CONTAINER" \
  | grep -vxF -e "$APP_DATA_DEST" -e "$APP_SOCKET_DEST" -e '' || true)
[[ -z $extra_mounts ]] \
  || ql_die "$APP_CONTAINER has mounts the Quadlet unit does not reproduce: $(tr '\n' ' ' <<<"$extra_mounts"); add them to quadlet/podman-mcp-admin.container first, or remove them"

# Privilege: the legacy container runs as container root with no added capabilities. The
# Quadlet unit adds DropCapability=ALL, NoNewPrivileges and ReadOnly, i.e. it narrows. A legacy
# container that was given MORE than the unit can express must be refused, not quietly reduced.
legacy_caps=$(podman inspect --format '{{range .HostConfig.CapAdd}}{{println .}}{{end}}' "$APP_CONTAINER" | grep -v '^$' || true)
[[ -z $legacy_caps ]] \
  || ql_die "$APP_CONTAINER was created with added capabilities ($(tr '\n' ' ' <<<"$legacy_caps")); the Quadlet unit drops ALL capabilities and would be a functional change"
[[ $(podman inspect --format '{{.HostConfig.Privileged}}' "$APP_CONTAINER") != true ]] \
  || ql_die "$APP_CONTAINER is --privileged; the Quadlet unit is not, and this script will not silently narrow that"
legacy_user=$(podman inspect --format '{{.Config.User}}' "$APP_CONTAINER")
[[ -z $legacy_user ]] \
  || ql_warn "$APP_CONTAINER runs as user '$legacy_user'; the Quadlet unit runs as container root (the host user in the rootless userns), which is what can open the podman socket"

# Exposure and port.
legacy_pub=$(app_published "$APP_CONTAINER") \
  || ql_die "$APP_CONTAINER publishes no host port for 8080/tcp; the Quadlet unit always publishes one"
legacy_bind=${legacy_pub%% *} legacy_port=${legacy_pub##* }
[[ -n $legacy_port ]] || ql_die "$APP_CONTAINER publishes no host port for 8080/tcp"
port=${port:-$legacy_port}
ql_assert_match "the publish port" "$port" '[1-9][0-9]{0,4}'
if [[ -z $bind ]]; then
  if ((keep_exposure)); then
    bind=$legacy_bind
  elif [[ $legacy_bind == 0.0.0.0 || $legacy_bind == '' ]]; then
    bind=127.0.0.1
  else
    bind=$legacy_bind
  fi
fi
if [[ $bind != "$legacy_bind" ]]; then
  ql_warn "DELIBERATE NARROWING: the legacy container publishes $legacy_bind:$legacy_port, the new unit will publish $bind:$port."
  ql_warn "This container holds the rootless podman socket: anything that reaches it controls every container of this user."
  ql_warn "Keep the old exposure with --keep-exposure (or --bind $legacy_bind) if a tunnel or proxy on ANOTHER host targets it."
fi
[[ $bind != 0.0.0.0 ]] || ql_die "MCP_ADMIN_BIND=0.0.0.0 is refused by install.sh; name one address"
# The port must be free, or held by the legacy container we are about to retire.
if command -v ss >/dev/null 2>&1 && [[ -n $(ss -ltnH "sport = :$port" 2>/dev/null || true) ]] \
  && [[ "$port" != "$legacy_port" ]]; then
  ql_die "port $port is already in use on this host (ss -ltnp 'sport = :$port'); pick another with --port"
fi

# The legacy unit(s).
legacy_unit='' legacy_unit_state=absent
for u in "${LEGACY_UNITS[@]}"; do
  if unit_exists "$u"; then
    legacy_unit=$u
    legacy_unit_state=$(systemctl --user is-enabled "$u" 2>/dev/null || echo disabled)
    break
  fi
done
if [[ -n $legacy_unit ]]; then
  ql_info "legacy unit $legacy_unit: $legacy_unit_state"
else
  ql_warn "no hand-written unit found (${LEGACY_UNITS[*]}); the legacy container will be stopped with podman stop"
fi
# A plain unit named exactly like the generated one shadows it: systemd prefers
# ~/.config/systemd/user over the generator. install.sh refuses to run with one in place, so
# the cutover moves it into the backup directory and --rollback puts it back.
shadow_file=''
if [[ -f $PLAIN_UNIT_DIR/$SHADOWING_UNIT || -L $PLAIN_UNIT_DIR/$SHADOWING_UNIT ]]; then
  shadow_file=$PLAIN_UNIT_DIR/$SHADOWING_UNIT
  grep -qs "$APP_CONTAINER" "$shadow_file" \
    || ql_die "$shadow_file exists, shadows the generated $APP_UNIT and does not mention $APP_CONTAINER: it belongs to something else. Resolve it by hand"
  ql_info "$shadow_file shadows the generated unit; the cutover moves it into the backup directory"
fi

# API version. The legacy openclaw container carries PODMAN_API_VERSION=v5.0.0 against a 4.9.3
# daemon; /data/config.json froze whatever the first boot saw, so this only matters for a fresh
# config, but a value the daemon cannot serve makes every tool call 404.
daemon_api=$(podman version --format '{{.Server.APIVersion}}' 2>/dev/null || true)
[[ $daemon_api == v* || -z $daemon_api ]] || daemon_api=v$daemon_api
legacy_api=$(app_env_of "$APP_CONTAINER" PODMAN_API_VERSION)
if [[ -z $api_version ]]; then
  api_version=${legacy_api:-$daemon_api}
  if [[ -n $legacy_api && -n $daemon_api && $legacy_api != "$daemon_api" ]] \
    && [[ $(printf '%s\n%s\n' "${legacy_api#v}" "${daemon_api#v}" | sort -V | tail -n1) == "${legacy_api#v}" ]]; then
    ql_warn "the legacy container asks for PODMAN_API_VERSION=$legacy_api but this daemon serves $daemon_api: using $daemon_api (override with --api-version $legacy_api)"
    api_version=$daemon_api
  fi
fi
[[ -n $api_version ]] || api_version=v4.9.3
ql_assert_match PODMAN_API_VERSION "$api_version" 'v[0-9]+\.[0-9]+\.[0-9]+'

# How the legacy container is kept for --rollback. Asked of this host, never of its name.
STRATEGY=$(ql_rollback_strategy "${LEGACY_ALL[@]}")
if [[ $STRATEGY == rename ]] && podman container exists "$APP_CONTAINER-legacy-$suffix"; then
  ql_die "$APP_CONTAINER-legacy-$suffix already exists; pick another --suffix"
fi
ql_info "legacy container $APP_CONTAINER on $legacy_bind:$legacy_port, image $(podman inspect --format '{{.ImageName}}' "$APP_CONTAINER"), restart policy $(ql_container_restart_policy "$APP_CONTAINER")"
ql_info "volume $APP_VOLUME: $VOL_ID (CreatedAt, mountpoint inode, config.json inode)"
ql_info "rollback strategy: $STRATEGY"

# derive_env <envfile>: the per-host settings the new units are rendered from, all taken from
# the running deployment rather than from this script's defaults.
derive_env() {
  local f=$1 pair k v
  ql_env_set "$f" MCP_ADMIN_BIND "$bind"
  ql_env_set "$f" MCP_ADMIN_PORT "$port"
  ql_env_set "$f" PODMAN_API_VERSION "$api_version"
  for pair in "${APP_CARRY_ENV[@]}"; do
    k=${pair%%:*}
    [[ $k != PODMAN_API_VERSION ]] || continue
    v=$(app_env_of "$APP_CONTAINER" "${pair#*:}")
    [[ -z $v ]] || ql_env_set "$f" "$k" "$v"
  done
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/$APP-migrate.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

if [[ $mode == dry-run ]]; then
  if [[ -f $ENV_FILE ]]; then install -m 600 -- "$ENV_FILE" "$WORK/$APP.env"; else install -m 600 -- "$REPO/$ENV_EXAMPLE_REL" "$WORK/$APP.env"; fi
  QL_ENV_MODE_CHECK=0 derive_env "$WORK/$APP.env"
  mkdir -p "$WORK/src" "$WORK/out"
  cp -p "$REPO"/quadlet/*.container "$REPO"/quadlet/*.volume "$REPO"/quadlet/*.network "$WORK/src/"
  ql_env_load "$WORK/$APP.env"
  ql_render "$WORK/src" "$WORK/$APP.env" "$REPO/quadlet/render-vars" "$WORK/out"
  ql_dryrun "$WORK/out" --verify --ref-dir "$QDIR" || ql_die "the rendered units failed the dry-run"
  if [[ $STRATEGY == capture ]]; then
    ql_info "dry-run: the cutover would stop ${legacy_unit:-the container}, capture $APP_CONTAINER into the backup directory and remove it (podman-restart.service would revive a renamed copy here), and install:"
  else
    ql_info "dry-run: the cutover would stop ${legacy_unit:-the container}, rename $APP_CONTAINER to $APP_CONTAINER-legacy-$suffix and install:"
  fi
  sed 's/^/    /' < <(grep -vE '^[[:space:]]*(#|$)' "$WORK/$APP.env") >&2
  exit 0
fi

# =============================================================================================
# 2. prepare (no downtime): env file, adopted secrets, image, backup, capture
# =============================================================================================
ql_info "step 2/4: env file, adopted secrets, image and a backup (no downtime)"
ql_enable_linger
ql_enable_podman_socket
ql_env_ensure "$REPO/$ENV_EXAMPLE_REL" "$ENV_FILE"
[[ $QL_ENV_CREATED != 1 ]] || ql_info "created $ENV_FILE; filling it in from the running container"
derive_env "$ENV_FILE"

# Secrets adopted from the legacy container's environment. The value goes from `podman inspect`
# into a shell variable and from there through ql_secret_ensure's pipe: never into argv, the
# journal or xtrace output. --update, so a re-run after a rotation converges.
for pair in "${APP_SECRETS[@]}"; do
  secret=${pair%%:*} var=${pair#*:}
  MIGRATE_SECRET_VALUE=$(app_env_of "$APP_CONTAINER" "$var")
  if [[ -n $MIGRATE_SECRET_VALUE ]]; then
    # shellcheck disable=SC2034 # read by ql_secret_ensure through env:MIGRATE_SECRET_VALUE
    ql_secret_ensure "$secret" env:MIGRATE_SECRET_VALUE --update
    ql_info "adopted $secret from the legacy container's $var (not printed)"
  else
    case $secret in
      podman-mcp-admin-jwt-secret) ql_secret_ensure "$secret" random:64 ;;
      podman-mcp-admin-password) ql_secret_ensure "$secret" random:24 ;;
      *) ql_secret_ensure "$secret" random:43 ;;
    esac
    ql_info "$var is not set on the legacy container; generated $secret (the value in /data/config.json stays authoritative)"
  fi
  MIGRATE_SECRET_VALUE=''
done
unset MIGRATE_SECRET_VALUE

# The image is built now, not during the downtime: a failed build then costs nothing.
if ! podman image exists "$IMAGE"; then
  ql_info "building $IMAGE (podman build --format docker; a few minutes on a small host)"
  podman build --format docker -t "$IMAGE" -f "$REPO/Dockerfile" "$REPO" \
    || ql_die "podman build failed; nothing was changed and the legacy deployment is untouched"
fi

bk=$(state_get BACKUP)
if [[ $(state_get STATUS) != prepared || ! -d $bk ]]; then
  bk=$(app_new_backup_dir "$BACKUP_ROOT/migrate-$(date +%Y%m%d-%H%M%S)")
fi
ql_backup_volume "$APP_VOLUME" "$bk" >/dev/null
podman inspect "$APP_CONTAINER" >"$bk/inspect.json"
chmod 600 -- "$bk/inspect.json"
if [[ -n $legacy_unit ]]; then systemctl --user cat "$legacy_unit" >"$bk/$legacy_unit" 2>/dev/null || true; fi
[[ -z $shadow_file ]] || cp -p -- "$shadow_file" "$bk/shadow-$SHADOWING_UNIT"
{
  printf 'legacy image: %s\n' "$(podman inspect --format '{{.ImageName}} {{.Image}}' "$APP_CONTAINER")"
  printf 'legacy publish: %s:%s\n' "$legacy_bind" "$legacy_port"
  printf 'legacy restart policy: %s\n' "$(ql_container_restart_policy "$APP_CONTAINER")"
  printf 'legacy unit: %s (%s)\n' "${legacy_unit:-none}" "$legacy_unit_state"
  printf 'volume identity: %s\n' "$VOL_ID"
  printf 'daemon api: %s ; chosen PODMAN_API_VERSION: %s\n' "${daemon_api:-?}" "$api_version"
  printf 'healthz before: %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://${legacy_bind/0.0.0.0/127.0.0.1}:$legacy_port/healthz" || echo 000)"
  printf 'config.json keys: %s\n' "$(podman exec "$APP_CONTAINER" python -c 'import json;print(",".join(sorted(json.load(open("/data/config.json")))))' 2>/dev/null || echo '?')"
} >"$bk/precheck.txt"
chmod 600 -- "$bk/precheck.txt"
state_set STATUS prepared
state_set BACKUP "$bk"
state_set SUFFIX "$suffix"
state_set LEGACY_UNIT "$legacy_unit"
state_set LEGACY_UNIT_STATE "$legacy_unit_state"
state_set LEGACY_HOST "${legacy_bind/0.0.0.0/127.0.0.1}"
state_set LEGACY_PORT "$legacy_port"
state_set VOLUME_IDENTITY "$VOL_ID"
# On the capture path the rollback copy is written now, while the legacy container still runs:
# one that cannot be replayed is refused before any downtime.
if [[ $STRATEGY == capture ]]; then app_legacy_capture "$bk" "${LEGACY_ALL[@]}"; fi
state_set STRATEGY "$STRATEGY"
app_write_checksums "$bk"
ql_info "backup with checksums in $bk (verify with: cd $bk && sha256sum -c SHA256SUMS)"
if [[ $mode == prepare ]]; then
  ql_info "prepared, with no downtime taken. Run the cutover with the same options minus --prepare-only"
  exit 0
fi

# =============================================================================================
# 3. cutover (downtime starts)
# =============================================================================================
confirm "the cutover stops the console for a few seconds"
ql_info "step 3/4: retiring the legacy deployment ($STRATEGY) and installing the Quadlet units"
state_set STATUS cutover
T0=$(now_ms)
# Two probes, because the publish address may change across the cutover (openclaw's 0.0.0.0 is
# narrowed to loopback by default). The reported downtime is the gap between the last answer of
# the old endpoint and the first answer of the new one.
PROBE_BEFORE=$bk/downtime-before.log PROBE_AFTER=$bk/downtime-after.log
app_probe_start "http://${legacy_bind/0.0.0.0/127.0.0.1}:$legacy_port/healthz" "$PROBE_BEFORE"
PROBE_BEFORE_PID=$APP_PROBE_PID
app_probe_start "http://$bind:$port/healthz" "$PROBE_AFTER"
PROBE_AFTER_PID=$APP_PROBE_PID
if [[ -n $legacy_unit ]]; then
  systemctl --user disable "$legacy_unit" >/dev/null 2>&1 || true
  systemctl --user stop "$legacy_unit" || true
  ! systemctl --user is-active --quiet "$legacy_unit" || ql_die "$legacy_unit is still active"
  ql_info "disabled and stopped $legacy_unit (the unit file stays on disk for --rollback)"
fi
if app_running "$APP_CONTAINER"; then podman stop -t 30 "$APP_CONTAINER" >/dev/null; fi
! app_running "$APP_CONTAINER" || ql_die "$APP_CONTAINER is still running"
if [[ -n $shadow_file ]]; then
  mv -f -- "$shadow_file" "$bk/shadow-$SHADOWING_UNIT"
  state_set SHADOW_SAVED "$bk/shadow-$SHADOWING_UNIT"
  systemctl --user daemon-reload
  ql_info "moved $shadow_file to $bk/shadow-$SHADOWING_UNIT (it would shadow the generated $APP_UNIT)"
fi
app_legacy_retire "$STRATEGY" "$([[ $STRATEGY == rename ]] && printf '%s' "$suffix")" "$bk" "${LEGACY_ALL[@]}"
app_write_checksums "$bk"

# =============================================================================================
# 4. install, verify, report the downtime
# =============================================================================================
ql_info "step 4/4: scripts/install.sh"
failed=0
"$REPO/scripts/install.sh" --no-build --yes || failed=1
T1=$(now_ms)
sleep 2 # let the second probe record the first successes after the cutover
app_probe_stop "$PROBE_BEFORE_PID"
app_probe_stop "$PROBE_AFTER_PID"
DOWN=$(app_probe_downtime_ms "$PROBE_BEFORE" "$PROBE_AFTER")
if ((!failed)); then
  after=$(app_volume_identity "$APP_VOLUME") || after='unreadable'
  if [[ $after == "$VOL_ID" ]]; then
    ql_info "data adopted: volume $APP_VOLUME is the same volume ($after)"
  else
    ql_warn "volume $APP_VOLUME changed across the cutover: before='$VOL_ID' after='$after'"
    ql_warn "a new CreatedAt or inode means Quadlet created a fresh empty volume instead of adopting the data"
    failed=1
  fi
fi
if ((failed)); then
  if ((auto_rollback)); then
    ql_warn "the cutover failed; rolling back automatically (--no-auto-rollback keeps it for inspection)"
    ASSUME_YES=1 rollback
    ql_die "migration failed and was rolled back; the legacy deployment serves again. Logs: journalctl --user -u $APP_UNIT -n 100"
  fi
  ql_die "the cutover failed; the new units are left in place. Inspect, then run: $0 --rollback"
fi
state_set STATUS "done"
state_set DOWNTIME_MS "$DOWN"
state_set CUTOVER_WALL_MS "$((T1 - T0))"
if ((DOWN < 0)); then
  ql_warn "downtime: the probes never saw both a last success and a first success; check $PROBE_BEFORE and $PROBE_AFTER by hand"
else
  ql_info "downtime: ${DOWN} ms of unreachable console (probed every 100 ms: last answer on $legacy_bind:$legacy_port -> first answer on $bind:$port)"
fi
ql_info "cutover wall clock: $(((T1 - T0) / 1000)) s (legacy stop -> install.sh, its health wait and tests/smoke.sh all finished)"
if [[ $STRATEGY == capture ]]; then
  ql_info "the legacy container was captured into $bk/legacy-container and removed (podman-restart.service is enabled here, so a renamed copy would have revived at boot)."
else
  ql_info "the legacy container is kept as $APP_CONTAINER-legacy-$suffix (stopped) and ${legacy_unit:-no unit} is disabled."
fi
ql_info "roll back with: $0 --rollback"
ql_warn "rotate the admin password and the connector token: they sat in the legacy container's environment and probably in shell history (README \"Migrating an existing deployment\")"
