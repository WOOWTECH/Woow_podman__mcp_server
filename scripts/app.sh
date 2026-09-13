# shellcheck shell=bash
# shellcheck disable=SC2034 # these settings are read by the scripts that source this file
# scripts/app.sh: facts and helpers shared by scripts/migrate-legacy.sh and tests/. Sourced
# after scripts/lib/quadlet-lib.sh, never executed. Nothing here starts or stops a service:
# the functions are the small, testable pieces the migration is built from, so
# tests/rollback-model.sh can drive them against the podman/systemctl doubles.
#
# The legacy deployment this describes is the one the old README documented and the one
# woowtechopenclaw runs today: a hand-made `podman run -d --name podman-mcp-admin
# --restart=unless-stopped -p 8080:8080 -v <socket>:/run/podman/podman.sock -v
# podman_mcp_data:/data -e JWT_SECRET=... -e ADMIN_PASSWORD=... localhost/podman-mcp-admin:latest`
# plus a hand-written `podman-podman-mcp-admin.service` whose ExecStart is `podman start -a`.

APP=podman-mcp-admin
APP_CONTAINER=podman-mcp-admin
APP_UNIT=podman-mcp-admin.service
APP_VOLUME=podman_mcp_data
APP_DATA_DEST=/data
APP_SOCKET_DEST=/run/podman/podman.sock
PODMAN_MIN=4.9
ENV_FILE=$HOME/.config/$APP/$APP.env
ENV_EXAMPLE_REL=config/$APP.env.example
APP_STATE_DIR=${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}/$APP
BACKUP_ROOT=$HOME/backups/$APP
# Units of the pre-Quadlet deployment, in the order migrate-legacy.sh retires them. The third
# name is the one that would SHADOW the generated unit, so it is handled separately.
LEGACY_UNITS=(podman-podman-mcp-admin.service container-podman-mcp-admin.service)
SHADOWING_UNIT=podman-mcp-admin.service
# Secrets install.sh reads, and the legacy environment variable each one is adopted from.
# MCP_AUTH_TOKEN is usually absent from the legacy container (the app generated and logged
# its own on first boot and froze it into /data/config.json, which the migration adopts).
APP_SECRETS=(
  podman-mcp-admin-jwt-secret:JWT_SECRET
  podman-mcp-admin-password:ADMIN_PASSWORD
  podman-mcp-admin-mcp-token:MCP_AUTH_TOKEN
)
# Non-secret settings carried from the legacy container's environment into the env file.
APP_CARRY_ENV=(
  PODMAN_MCP_PROFILE:PODMAN_MCP_PROFILE
  PODMAN_MCP_NAME_ALLOW:PODMAN_MCP_NAME_ALLOW
  PODMAN_API_VERSION:PODMAN_API_VERSION
  PODMAN_MCP_MAX_CHARS:PODMAN_MCP_MAX_CHARS
  JWT_EXPIRY_HOURS:JWT_EXPIRY_HOURS
)

app_running() { [[ $(podman inspect --format '{{.State.Running}}' "$1" 2>/dev/null) == true ]]; }

# app_spec_path <abs path>: rewrite $HOME/... as %h/... so a rendered unit carries no literal
# home path (systemd expands %h when it starts the unit).
app_spec_path() {
  local p=$1
  [[ $p == "$HOME"/* ]] && p="%h/${p#"$HOME"/}"
  printf '%s' "$p"
}

# app_env_of <container> <VAR>: one value from .Config.Env. The value is printed on stdout and
# nowhere else, so a caller can pipe it straight into `podman secret create` without it ever
# reaching argv, the journal or `set -x` output. Prints nothing when the variable is unset.
app_env_of() {
  local c=${1:?usage: app_env_of <container> <VAR>} k=${2:?} line
  while IFS= read -r line; do
    if [[ $line == "$k="* ]]; then printf '%s' "${line#"$k="}"; return 0; fi
  done < <(podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$c" 2>/dev/null)
  return 0
}

# app_published <container> <container-port>: prints "<bind> <hostport>" for one container
# port, read from the RESOLVED port bindings rather than from the create command (podman
# records an empty HostIP for `-p 8080:8080`, which means 0.0.0.0, and the migration has to see
# that so it can narrow the exposure deliberately instead of copying it by accident).
#
# The template is byte for byte the one ql_capture_container writes into the capture's `ports`
# file, so one shape is parsed everywhere. Its Go field is HostIP - the lowercase `HostIp` is
# only what podman PRINTS; naming it wrongly makes podman write "can't evaluate field" to
# stderr, nothing to stdout and exit 125, and a caller that only warns then carries on with its
# defaults (STANDARD section 8; it dropped port 30142 from an npm migration once).
app_published() {
  local c=${1:?usage: app_published <container> <port/proto>} want=${2:-8080/tcp} rows row ip port
  rows=$(podman inspect --format '{{range $p, $b := .NetworkSettings.Ports}}{{$p}}|{{range $b}}{{.HostIP}}:{{.HostPort}} {{end}}{{println}}{{end}}' "$c") || return 1
  row=$(sed -n "s#^${want}|##p" <<<"$rows" | tr ' ' '\n' | grep -m1 ':' ) || return 1
  ip=${row%:*} port=${row##*:}
  [[ -n $port ]] || return 1
  printf '%s %s' "${ip:-0.0.0.0}" "$port"
}

# app_mount_source <container> <destination>: the host source of one mount
app_mount_source() {
  podman inspect --format '{{range .Mounts}}{{.Destination}}|{{.Source}}{{println}}{{end}}' "$1" 2>/dev/null \
    | sed -n "s#^$2|##p" | tail -n1
}

# app_volume_identity <volume>: "<CreatedAt> <mountpoint-inode> <config.json-inode>" - the
# fingerprint the migration records before the cutover and asserts again afterwards. Quadlet
# adopts a volume by NAME; that is only worth trusting if the name still resolves to the same
# directory. A recreated volume gets a new CreatedAt and a new inode, so comparing the two
# strings proves the data was adopted rather than re-created empty.
# config.json is absent on a never-started deployment: "-" then, which still compares equal
# across the cutover.
app_volume_identity() {
  local v=${1:?usage: app_volume_identity <volume>} mp created ino cfg='-'
  mp=$(podman volume inspect --format '{{.Mountpoint}}' "$v" 2>/dev/null) || return 1
  created=$(podman volume inspect --format '{{.CreatedAt}}' "$v" 2>/dev/null) || return 1
  [[ -n $mp ]] || return 1
  ino=$(podman unshare stat -c '%i' -- "$mp" 2>/dev/null) || ino='?'
  if podman unshare test -f "$mp/config.json" 2>/dev/null; then
    cfg=$(podman unshare stat -c '%i' -- "$mp/config.json" 2>/dev/null) || cfg='?'
  fi
  printf '%s %s %s' "$created" "$ino" "$cfg"
}

# app_new_backup_dir [dir]: a fresh private directory (0700). Without an argument it is
# ~/backups/podman-mcp-admin/<timestamp>, with -2, -3, ... when several land in the same second.
app_new_backup_dir() {
  local d=${1:-} base i=2
  if [[ -z $d ]]; then
    base=$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)
    d=$base
    while [[ -e $d ]]; do d=$base-$i; i=$((i + 1)); done
  fi
  [[ ! -e $d ]] || ql_die "$d already exists"
  (umask 077 && mkdir -p -- "$d") || ql_die "cannot create $d"
  printf '%s' "$d"
}

# app_write_checksums <dir>: SHA256SUMS over every file in the backup, in `sha256sum -c`
# format, so `sha256sum -c SHA256SUMS` inside the directory verifies a restore later.
app_write_checksums() {
  local d=${1:?usage: app_write_checksums <dir>} list
  list=$(cd -- "$d" && find . -type f ! -name 'SHA256SUMS*' ! -name '*.sha256' -printf '%P\n' | LC_ALL=C sort)
  [[ -n $list ]] || return 0
  (cd -- "$d" && printf '%s\n' "$list" | tr '\n' '\0' | xargs -0 sha256sum >SHA256SUMS.partial) \
    || { rm -f -- "$d/SHA256SUMS.partial"; ql_die "cannot checksum $d"; }
  mv -f -- "$d/SHA256SUMS.partial" "$d/SHA256SUMS"
  chmod 600 -- "$d/SHA256SUMS"
}

# app_legacy_capture <backup dir> <container>...: write the rollback copy of each container.
# Read-only towards the containers, so it belongs in the prepare phase, before any downtime: a
# container the library cannot replay (an empty CreateCommand - created through the podman API
# rather than the CLI) is refused here, while the legacy deployment is still serving.
app_legacy_capture() {
  local bk=${1:?usage: app_legacy_capture <backup dir> <container>...} c meta
  shift
  for c in "$@"; do
    meta=$bk/legacy-container/$c/meta
    if [[ -f $meta ]]; then
      ql_info "the rollback copy of $c is already in $bk/legacy-container/$c"
    else
      ql_capture_container "$c" "$bk" >/dev/null
    fi
    [[ $(sed -n 's/^RECREATABLE=//p' "$meta" | tail -n1) == 1 ]] || ql_die \
      "$c was created through the podman API, not the CLI, so its create command cannot be replayed and a capture-based rollback is impossible. Either disable podman-restart.service (then the legacy container can simply be renamed) or plan to rebuild $c by hand from $bk/legacy-container/$c/inspect.json"
  done
}

# app_legacy_retire <strategy> <suffix> <backup dir> <container>...: take the legacy containers
# out of the new units' way, in the shape ql_rollback_strategy asked for.
app_legacy_retire() {
  # The suffix is empty on the capture path: nothing is renamed there, so there is no
  # <name>-legacy-<suffix> to name. ${2-} rather than ${2:?}, which would abort the script.
  local strategy=${1:?} sfx=${2-} bk=${3:?} c
  shift 3
  for c in "$@"; do
    case $strategy in
      rename)
        [[ -n $sfx ]] || ql_die "the rename path needs a suffix for $c-legacy-<suffix>"
        podman rename "$c" "$c-legacy-$sfx" || ql_die "podman rename $c failed"
        ql_info "renamed $c -> $c-legacy-$sfx (kept, stopped, for --rollback)"
        ;;
      capture)
        [[ -f $bk/legacy-container/$c/meta ]] \
          || ql_die "no rollback copy of $c in $bk; capture it before removing it"
        # A plain `podman rm`. NEVER `rm -v`: that deletes the anonymous volumes the capture
        # recorded and expects to find again at rollback time.
        podman rm "$c" >/dev/null || ql_die "podman rm $c failed"
        ql_info "removed $c; the rollback copy is $bk/legacy-container/$c"
        ;;
      *) ql_die "unknown rollback strategy '$strategy'" ;;
    esac
  done
}

# app_legacy_restore <suffix> <backup dir> <container>...: bring the legacy containers back,
# whichever shape the cutover used. A recreated container comes back stopped and with its
# original restart policy; the caller starts it, exactly as it starts a renamed one.
app_legacy_restore() {
  # An empty suffix means the cutover captured rather than renamed: there is no
  # <name>-legacy-<suffix> to look for, only the rollback copy.
  local sfx=${1-} bk=${2:?} c
  shift 2
  for c in "$@"; do
    if [[ -n $sfx ]] && podman container exists "$c-legacy-$sfx"; then
      podman rename "$c-legacy-$sfx" "$c" || ql_die "podman rename $c-legacy-$sfx failed"
      ql_info "renamed $c-legacy-$sfx -> $c"
    elif [[ -f $bk/legacy-container/$c/meta ]]; then
      ql_recreate_container "$bk" "$c" >/dev/null || ql_die "could not recreate $c from $bk"
      ql_info "recreated $c from $bk/legacy-container/$c (stopped, with its original restart policy)"
    else
      ql_die "neither the renamed container ${sfx:+$c-legacy-$sfx }nor a rollback copy in $bk exists; restore $c by hand"
    fi
  done
}
