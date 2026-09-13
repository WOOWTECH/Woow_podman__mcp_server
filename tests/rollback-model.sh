#!/usr/bin/env bash
# tests/rollback-model.sh: pins the behaviour of scripts/migrate-legacy.sh - the rollback model
# it uses to keep the legacy container available (STANDARD 7a), and the facts it reads off a
# running deployment before it touches anything.
#
#   tests/rollback-model.sh [name-filter]
#
# podman and systemctl are the doubles in tests/shims, placed first on PATH; every test gets
# its own HOME and shim state. No container is created and the real user manager is never
# touched. Two host shapes are modelled:
#   toypark1234      podman-restart.service disabled -> rename and leave stopped
#   woowtechopenclaw podman-restart.service enabled. Today's podman-mcp-admin is
#                    `unless-stopped`, so even there the answer is `rename`; that is a fact
#                    about the container, not about the repo, so the `always` shape - which
#                    WOULD revive at the next boot and fight the Quadlet container for the
#                    name, the port and the podman socket - is tested too.
#
# Every test runs in its own subshell on purpose (isolated HOME, shim state, env), so the
# "modified in a subshell" notes do not apply here:
# shellcheck disable=SC2030,SC2031
# shellcheck source-path=SCRIPTDIR
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO=$(cd "$HERE/.." && pwd -P)
SHIMS=$HERE/shims
FILTER=${1:-}
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/mcp-rollback-tests.XXXXXX")
trap 'rm -rf "$ROOT"' EXIT
npass=0 nfail=0
FAILED=()

die_t() { printf 'ASSERTION FAILED: %s\n' "$*" >&2; exit 1; }
eq() { [[ $1 == "$2" ]] || die_t "${3:-value}: expected [$2] got [$1]"; }
has() { [[ $1 == *"$2"* ]] || die_t "${3:-output} lacks [$2] in:"$'\n'"$1"; }
hasnt() { [[ $1 != *"$2"* ]] || die_t "${3:-output} must not contain [$2] in:"$'\n'"$1"; }
calls() { cat "$SHIM_STATE/calls"; }
ncalls() { grep -cF -- "$1" "$SHIM_STATE/calls" || true; }
OUT=''
expect_ok() { OUT=$( ("$@") 2>&1) || die_t "expected success of: $*"$'\n'"$OUT"; }
expect_fail() { if OUT=$( ("$@") 2>&1); then die_t "expected failure of: $*"$'\n'"$OUT"; fi; }

# ---- fixtures ---------------------------------------------------------------------------
# mk_legacy <policy>: the hand-made podman-mcp-admin of the old README. Its CreateCommand is
# the literal `podman run` an operator typed, including --restart, -p 8080:8080, the socket
# bind and the two secret -e flags. An empty HostIP in the port binding is what podman records
# for `-p 8080:8080`, i.e. 0.0.0.0.
mk_legacy() {
  local policy=${1:-unless-stopped} name=podman-mcp-admin image=localhost/podman-mcp-admin:latest d
  d=$SHIM_STATE/containers/$name
  mkdir -p "$d" "$SHIM_STATE/image-ids" "$SHIM_STATE/volumes/podman_mcp_data" "$SHIM_STATE/vol-created"
  printf '%s' "$policy" >"$d/policy"
  printf '0' >"$d/retries"
  printf 'cid-mcp' >"$d/id"
  printf '%s' "$image" >"$d/image"
  printf 'imgid-mcp' >"$d/image_id"
  printf 'imgid-mcp' >"$SHIM_STATE/image-ids/${image//[\/:@]/_}"
  printf 'slirp4netns' >"$d/netmode"
  printf 'false' >"$d/autoremove"
  : >"$d/project"
  : >"$d/service"
  printf '4096' >"$d/sizerw"
  printf 'volume|podman_mcp_data|%s/volumes/podman_mcp_data|/data|true|rprivate\n' "$SHIM_STATE" >"$d/mounts"
  printf 'bind||/run/user/1000/podman/podman.sock|/run/podman/podman.sock|true|rprivate\n' >>"$d/mounts"
  : >"$d/networks"
  # an empty HostIP is exactly what `-p 8080:8080` records
  printf '8080/tcp|:8080 \n' >"$d/ports"
  : >"$d/labels"
  : >"$d/label"
  cat >"$d/env" <<'ENV'
PATH=/usr/local/bin:/usr/bin
JWT_SECRET=jwt-from-the-legacy-container
ADMIN_PASSWORD=pw-from-the-legacy-container
JWT_EXPIRY_HOURS=12
PODMAN_URI=unix:///run/podman/podman.sock
PODMAN_API_VERSION=v5.0.0
PODMAN_MCP_PROFILE=safe
PODMAN_MCP_MAX_CHARS=20000
ENV
  printf '2026-08-06T00:00:00Z\n' >"$SHIM_STATE/vol-created/podman_mcp_data"
  printf 'seed\n' >"$SHIM_STATE/volumes/podman_mcp_data/config.json"
  printf '%s\0' /usr/bin/podman run -d --name podman-mcp-admin --restart="$policy" \
    -p 8080:8080 -e JWT_SECRET=jwt-from-the-legacy-container -e ADMIN_PASSWORD=pw-from-the-legacy-container \
    -e PODMAN_MCP_PROFILE=safe -e PODMAN_API_VERSION=v5.0.0 \
    -v /run/user/1000/podman/podman.sock:/run/podman/podman.sock -v podman_mcp_data:/data \
    "$image" >"$d/createcommand.argv0"
}
# mk_api_created: same container, but created through the podman API (no CreateCommand)
mk_api_created() {
  mk_legacy "$1"
  : >"$SHIM_STATE/containers/podman-mcp-admin/createcommand.argv0"
}
enable_restart_unit() { # what woowtechopenclaw looks like
  mkdir -p "$SHIM_STATE/units/podman-restart.service"
  echo enabled >"$SHIM_STATE/units/podman-restart.service/UnitFileState"
}

# ---- the strategy decision ----------------------------------------------------------------
t_a_disabled_restart_unit_keeps_the_rename_path() {
  mk_legacy unless-stopped
  eq "$(ql_rollback_strategy podman-mcp-admin 2>/dev/null)" rename "strategy on a toypark-like host"
  expect_ok app_legacy_retire rename 20260914 "$T/bk" podman-mcp-admin
  has "$OUT" "renamed podman-mcp-admin -> podman-mcp-admin-legacy-20260914"
  eq "$(ncalls 'podman rm ')" 0 "nothing is removed on the rename path"
  [[ ! -d $T/bk/legacy-container ]] || die_t "the rename path must not write a capture"
  expect_ok app_legacy_restore 20260914 "$T/bk" podman-mcp-admin
  has "$OUT" "renamed podman-mcp-admin-legacy-20260914 -> podman-mcp-admin"
  podman container exists podman-mcp-admin || die_t "the rollback did not bring the container back"
  eq "$(ncalls 'podman create')" 0 "a renamed container is not recreated"
}

t_openclaws_unless_stopped_container_is_still_the_rename_path() {
  # The live fact this repo must not hardcode either way: podman-restart.service IS enabled on
  # woowtechopenclaw, but its filter matches the policy string exactly, and podman-mcp-admin is
  # `unless-stopped`. So the answer there today is `rename`, decided from the host.
  enable_restart_unit
  mk_legacy unless-stopped
  eq "$(ql_rollback_strategy podman-mcp-admin 2>/dev/null)" rename "unless-stopped is never revived by the restart unit"
}

t_an_always_container_on_an_enabled_host_takes_the_capture_path() {
  enable_restart_unit
  mk_legacy always
  eq "$(ql_rollback_strategy podman-mcp-admin 2>/dev/null)" capture "strategy when the container would revive at boot"
  expect_ok app_legacy_capture "$T/bk" podman-mcp-admin
  [[ -s $T/bk/legacy-container/podman-mcp-admin/meta ]] || die_t "no capture written"
  eq "$(sed -n 's/^RECREATABLE=//p' "$T/bk/legacy-container/podman-mcp-admin/meta")" 1 "recreatable"
  eq "$(sed -n 's/^RESTART_POLICY=//p' "$T/bk/legacy-container/podman-mcp-admin/meta")" always "policy recorded"
  # capturing is read-only: the legacy console is still serving at this point
  eq "$(ncalls 'podman rm ')" 0 "the capture removes nothing"
  eq "$(ncalls 'podman rename')" 0 "the capture renames nothing"
  expect_ok app_legacy_retire capture '' "$T/bk" podman-mcp-admin
  has "$OUT" "removed podman-mcp-admin;"
  podman container exists podman-mcp-admin && die_t "the container was not removed"
  expect_ok app_legacy_restore '' "$T/bk" podman-mcp-admin
  has "$OUT" "recreated podman-mcp-admin"
  eq "$(ql_container_restart_policy podman-mcp-admin)" always "the original restart policy comes back"
  return 0
}

t_the_capture_path_never_removes_the_data_volume() {
  # `podman rm -v` would delete podman_mcp_data - the admin password and the connector token.
  enable_restart_unit
  mk_legacy always
  expect_ok app_legacy_capture "$T/bk" podman-mcp-admin
  expect_ok app_legacy_retire capture '' "$T/bk" podman-mcp-admin
  hasnt "$(calls)" "podman rm -v" "rm -v would delete the volume the new unit adopts"
  hasnt "$(calls)" "podman rm --volumes" "rm --volumes would delete the volume the new unit adopts"
  podman volume exists podman_mcp_data || die_t "podman_mcp_data is gone"
}

t_capture_refuses_a_container_the_library_cannot_replay() {
  enable_restart_unit
  mk_api_created always
  expect_fail app_legacy_capture "$T/bk" podman-mcp-admin
  has "$OUT" "podman API"
  eq "$(ncalls 'podman rm ')" 0 "a refused capture removes nothing"
}

t_retire_refuses_to_remove_without_a_capture() {
  enable_restart_unit
  mk_legacy always
  expect_fail app_legacy_retire capture '' "$T/bk" podman-mcp-admin
  has "$OUT" "no rollback copy of podman-mcp-admin"
  eq "$(ncalls 'podman rm ')" 0 "nothing is removed without a capture"
}

t_capture_is_idempotent_between_prepare_only_and_the_cutover() {
  enable_restart_unit
  mk_legacy always
  expect_ok app_legacy_capture "$T/bk" podman-mcp-admin # --prepare-only
  expect_ok app_legacy_capture "$T/bk" podman-mcp-admin # the cutover reuses the same backup dir
  has "$OUT" "already in"
}

# ---- the facts the pre-flight reads --------------------------------------------------------
t_the_published_address_is_read_as_0_0_0_0_not_as_empty() {
  # `-p 8080:8080` records an EMPTY HostIP. Reading it as "" and rendering PublishPort=:8080
  # would keep openclaw's LAN exposure of the podman socket by accident; the migration has to
  # see 0.0.0.0 so it can narrow it deliberately and say so.
  mk_legacy unless-stopped
  eq "$(app_published podman-mcp-admin)" "0.0.0.0 8080" "the legacy publish"
}

t_the_port_template_uses_the_go_field_name_HostIP() {
  # STANDARD section 8: the Go field is HostIP; the lowercase HostIp is only what podman
  # PRINTS. A wrong name makes podman exit 125 with nothing on stdout, and a script that only
  # warns then silently carries on with its defaults (it dropped port 30142 once already).
  grep -q '{{.HostIP}}:{{.HostPort}}' "$REPO/scripts/app.sh" || die_t "scripts/app.sh does not use {{.HostIP}}"
  grep -v '^[[:space:]]*#' "$REPO/scripts/app.sh" | grep -q 'HostIp' && die_t "scripts/app.sh uses the JSON tag HostIp"
  return 0
}

t_secrets_and_settings_are_read_off_the_running_container() {
  mk_legacy unless-stopped
  eq "$(app_env_of podman-mcp-admin JWT_SECRET)" jwt-from-the-legacy-container "adopted JWT_SECRET"
  eq "$(app_env_of podman-mcp-admin ADMIN_PASSWORD)" pw-from-the-legacy-container "adopted ADMIN_PASSWORD"
  eq "$(app_env_of podman-mcp-admin PODMAN_MCP_PROFILE)" safe "carried profile"
  eq "$(app_env_of podman-mcp-admin PODMAN_API_VERSION)" v5.0.0 "carried API version"
  eq "$(app_env_of podman-mcp-admin MCP_AUTH_TOKEN)" '' "an unset variable prints nothing"
}

t_the_volume_fingerprint_changes_when_the_volume_is_re_created() {
  # The whole point of the post-cutover assertion: a name that resolves to a NEW, empty volume
  # must not read as "adopted". Same name, new CreatedAt and a new config.json inode -> a
  # different fingerprint.
  mk_legacy unless-stopped
  before=$(app_volume_identity podman_mcp_data)
  [[ -n $before ]] || die_t "no fingerprint"
  eq "$(app_volume_identity podman_mcp_data)" "$before" "the fingerprint of an untouched volume is stable"
  rm -rf "$SHIM_STATE/volumes/podman_mcp_data"
  mkdir -p "$SHIM_STATE/volumes/podman_mcp_data"
  printf 'fresh\n' >"$SHIM_STATE/volumes/podman_mcp_data/config.json"
  printf '2026-09-14T00:00:00Z\n' >"$SHIM_STATE/vol-created/podman_mcp_data"
  [[ $(app_volume_identity podman_mcp_data) != "$before" ]] \
    || die_t "a re-created volume must not fingerprint the same as the adopted one"
}

t_the_backup_carries_verifiable_checksums() {
  mkdir -p "$T/bk/legacy-container/podman-mcp-admin"
  printf 'a\n' >"$T/bk/inspect.json"
  printf 'b\n' >"$T/bk/legacy-container/podman-mcp-admin/meta"
  expect_ok app_write_checksums "$T/bk"
  [[ -f $T/bk/SHA256SUMS ]] || die_t "no SHA256SUMS"
  (cd "$T/bk" && sha256sum -c SHA256SUMS >/dev/null 2>&1) || die_t "the checksums do not verify"
  eq "$(stat -c '%a' "$T/bk/SHA256SUMS")" 600 "SHA256SUMS mode"
  printf 'tampered\n' >"$T/bk/inspect.json"
  (cd "$T/bk" && sha256sum -c SHA256SUMS >/dev/null 2>&1) && die_t "a tampered backup still verified"
  return 0
}

# ---- structural: the script really uses this model ------------------------------------------
t_migrate_legacy_asks_the_host_instead_of_hardcoding() {
  local f=$REPO/scripts/migrate-legacy.sh
  grep -q 'ql_rollback_strategy' "$f" || die_t "scripts/migrate-legacy.sh does not ask ql_rollback_strategy"
  grep -q 'is-enabled podman-restart.service' "$f" && die_t "it decides on podman-restart.service by itself"
  grep -q 'app_legacy_retire' "$f" || die_t "the cutover does not go through app_legacy_retire"
  grep -q 'app_legacy_restore' "$f" || die_t "the rollback does not go through app_legacy_restore"
  grep -q 'app_volume_identity' "$f" || die_t "the cutover does not prove the volume was adopted"
  grep -q 'app_write_checksums' "$f" || die_t "the backup is not checksummed"
  grep -q 'DOWNTIME_MS' "$f" || die_t "the downtime is not measured"
  return 0
}

t_the_migration_never_removes_the_volume_or_bulk_prunes() {
  local f=$REPO/scripts/migrate-legacy.sh
  grep -qE 'podman (volume rm|volume prune|system prune|system reset)' "$f" \
    && die_t "scripts/migrate-legacy.sh contains a data-destroying podman command"
  grep -qE 'podman rm (-v|--volumes)' "$f" && die_t "scripts/migrate-legacy.sh uses rm -v"
  return 0
}

run() {
  local t=$1 log rc
  [[ -z $FILTER || $t == *"$FILTER"* ]] || return 0
  log=$ROOT/$t.log
  (
    set -euo pipefail
    T=$ROOT/$t
    mkdir -p "$T/home" "$T/state" "$T/run" "$T/bk" "$T/empty"
    export HOME=$T/home SHIM_STATE=$T/state XDG_RUNTIME_DIR=$T/run USER=tester TMPDIR=$T
    export PATH="$SHIMS:$PATH" QL_POLL_INTERVAL=0.05 QL_LOG_PREFIX=rollback-model
    unset QL_DRY_RUN QL_STATE_ROOT QL_QUADLET_DIR QL_CONFIG_ROOT
    : >"$SHIM_STATE/calls"
    [[ $(command -v podman) == "$SHIMS/podman" && $(command -v systemctl) == "$SHIMS/systemctl" ]] \
      || die_t "the shims are not first on PATH; refusing to run"
    # shellcheck source=../scripts/lib/quadlet-lib.sh
    . "$REPO/scripts/lib/quadlet-lib.sh"
    # shellcheck source=../scripts/app.sh
    . "$REPO/scripts/app.sh"
    "$t"
  ) >"$log" 2>&1
  rc=$?
  if ((rc == 0)); then
    npass=$((npass + 1))
    printf 'ok    %s\n' "$t"
  else
    nfail=$((nfail + 1))
    FAILED+=("$t")
    printf 'FAIL  %s\n' "$t"
    tail -n 25 "$log" | sed 's/^/      | /'
  fi
}

for t in $(declare -F | sed -n 's/^declare -f \(t_.*\)$/\1/p'); do run "$t"; done
printf '\n%d passed, %d failed\n' "$npass" "$nfail"
((nfail == 0)) || { printf 'failed: %s\n' "${FAILED[*]}"; exit 1; }
