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
# Some assertions grep for literal shell text inside another script:
# shellcheck disable=SC2016
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
# The two values the migration adopts out of the legacy container. Held in variables so this
# file never carries a literal secret-shaped assignment (tests/dryrun.local.sh refuses one).
LEGACY_CID=3f2a9c1d5e7b0a4c6d8e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e
LEGACY_JWT=jwt-from-the-legacy-container
LEGACY_PW=pw-from-the-legacy-container
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
  # a real podman container id: hex, and what the transient healthcheck timer is named after
  printf '%s' "$LEGACY_CID" >"$d/id"
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
  # The two adopted values are written through printf rather than spelled into this file:
  # tests/dryrun.local.sh refuses a committed KEY=<value> in credential shape, and it is right
  # to refuse it even when the value is obviously fake.
  cat >"$d/env" <<ENV
PATH=/usr/local/bin:/usr/bin
$(printf 'JWT_SECRET=%s' "$LEGACY_JWT")
$(printf 'ADMIN_PASSWORD=%s' "$LEGACY_PW")
JWT_EXPIRY_HOURS=12
PODMAN_URI=unix:///run/podman/podman.sock
PODMAN_API_VERSION=v5.0.0
PODMAN_MCP_PROFILE=safe
PODMAN_MCP_MAX_CHARS=20000
ENV
  printf '2026-08-06T00:00:00Z\n' >"$SHIM_STATE/vol-created/podman_mcp_data"
  printf 'seed\n' >"$SHIM_STATE/volumes/podman_mcp_data/config.json"
  printf '%s\0' /usr/bin/podman run -d --name podman-mcp-admin --restart="$policy" \
    -p 8080:8080 -e "JWT_SECRET=$LEGACY_JWT" -e "ADMIN_PASSWORD=$LEGACY_PW" \
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

# The renamed legacy container must not keep a healthcheck timer. podman keys that timer on
# the container ID, so `podman rename` does not detach it: it goes on firing
# `podman healthcheck run <id>` every interval against a container that is no longer running,
# and the transient <id>.service fails. STANDARD.md makes an empty `systemctl --user --failed`
# the cutover gate, so one leftover timer keeps the soak red for ever.
t_retiring_a_legacy_container_stops_its_healthcheck_timer() {
  mk_legacy unless-stopped
  expect_ok app_legacy_retire rename 20260914 "$T/bk" podman-mcp-admin
  has "$OUT" "stopped the healthcheck timer of podman-mcp-admin"
  has "$(calls)" "systemctl --user stop $LEGACY_CID.timer $LEGACY_CID.service" \
    "the timer must be stopped by container id"
  # and while the old name still resolves, i.e. before the rename
  local t_line r_line
  t_line=$(grep -n -F -m1 "systemctl --user stop $LEGACY_CID.timer" "$SHIM_STATE/calls" | cut -d: -f1)
  r_line=$(grep -n -F -m1 'podman rename podman-mcp-admin ' "$SHIM_STATE/calls" | cut -d: -f1)
  ((t_line > 0 && r_line > 0 && t_line < r_line)) \
    || die_t "the timer must be stopped before the rename (timer at line ${t_line:-none}, rename at ${r_line:-none})"
  # the capture path removes the container, so its timer has to go too
  : >"$SHIM_STATE/calls"
  enable_restart_unit
  mk_legacy always
  expect_ok app_legacy_capture "$T/bk2" podman-mcp-admin
  expect_ok app_legacy_retire capture '' "$T/bk2" podman-mcp-admin
  has "$(calls)" "systemctl --user stop $LEGACY_CID.timer $LEGACY_CID.service" \
    "the capture path must stop the timer as well"
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
  eq "$(app_env_of podman-mcp-admin JWT_SECRET)" "$LEGACY_JWT" "adopted JWT_SECRET"
  eq "$(app_env_of podman-mcp-admin ADMIN_PASSWORD)" "$LEGACY_PW" "adopted ADMIN_PASSWORD"
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

# ---- the downtime report ----------------------------------------------------------------------
t_the_downtime_spans_the_old_endpoint_to_the_new_one() {
  # A migration may change the publish address (openclaw's 0.0.0.0 is narrowed to loopback), so
  # one probe cannot see both sides. The reported downtime is the last answer of the OLD
  # endpoint to the first answer of the NEW one.
  printf '1000 200\n1100 200\n1200 000\n1300 000\n' >"$T/before.log"
  printf '1150 000\n1250 000\n1500 200\n1600 200\n' >"$T/after.log"
  eq "$(app_probe_downtime_ms "$T/before.log" "$T/after.log")" 400 "1100 -> 1500"
  # a cutover with no observed gap reports 0, never a negative number
  printf '1000 200\n1600 200\n' >"$T/b2.log"
  printf '1500 200\n' >"$T/a2.log"
  eq "$(app_probe_downtime_ms "$T/b2.log" "$T/a2.log")" 0 "an overlap is not negative downtime"
  # and a side that never answered must not round to 0
  printf '1000 000\n' >"$T/b3.log"
  eq "$(app_probe_downtime_ms "$T/b3.log" "$T/after.log")" -1 "no success before the cutover"
  eq "$(app_probe_downtime_ms "$T/before.log" "$T/b3.log")" -1 "no success after the cutover"
  # a password-protected endpoint answers 401 and is still up
  printf '1000 401\n' >"$T/b4.log"
  printf '1300 401\n' >"$T/a4.log"
  eq "$(app_probe_downtime_ms "$T/b4.log" "$T/a4.log")" 300 "401 counts as up"
}

t_the_probe_measures_a_real_restart() {
  # End to end against a real HTTP server on loopback that the test stops and starts again on a
  # DIFFERENT port, which is what a narrowed publish address looks like to the probes.
  command -v python3 >/dev/null 2>&1 || { printf 'skip: no python3\n'; return 0; }
  local p1 p2 srv b a
  p1=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
  p2=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
  cd "$T"
  python3 -m http.server "$p1" --bind 127.0.0.1 >/dev/null 2>&1 &
  srv=$!
  sleep 0.6
  app_probe_start "http://127.0.0.1:$p1/" "$T/live-before.log"; b=$APP_PROBE_PID
  app_probe_start "http://127.0.0.1:$p2/" "$T/live-after.log"; a=$APP_PROBE_PID
  sleep 0.5
  kill "$srv" 2>/dev/null || true; wait "$srv" 2>/dev/null || true
  sleep 0.6
  python3 -m http.server "$p2" --bind 127.0.0.1 >/dev/null 2>&1 &
  srv=$!
  sleep 1.0
  app_probe_stop "$b"; app_probe_stop "$a"
  kill "$srv" 2>/dev/null || true; wait "$srv" 2>/dev/null || true
  d=$(app_probe_downtime_ms "$T/live-before.log" "$T/live-after.log")
  ((d >= 300 && d <= 2500)) || die_t "measured downtime $d ms is outside the ~600 ms outage the test created"
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
  # The probe logs live in the backup directory and are still being written when step 3
  # checksums it: the sums must be regenerated after the probes are stopped, or every
  # migration ends with two FAILED lines in `sha256sum -c SHA256SUMS`.
  awk '/app_probe_stop/{seen=1} seen && /app_write_checksums/{ok=1} END{exit !ok}' "$f" \
    || die_t "the checksums are never rewritten after the downtime probes are stopped"
  grep -q 'DOWNTIME_MS' "$f" || die_t "the downtime is not measured"
  grep -q 'app_probe_downtime_ms "$PROBE_BEFORE" "$PROBE_AFTER"' "$f" \
    || die_t "the downtime is not the gap the two probes measured"
  return 0
}

# ---- the per-app lock, as quadlet-lib 1.5.0 defines it ---------------------------------------
# Up to 1.4.0 the lock was an flock on a descriptor opened with `exec {fd}>`, which bash does not
# mark close-on-exec. Rootless podman leaves children behind by design - conmon, slirp4netns,
# rootlessport, the catatonit pause process - and every one of them inherited that descriptor and
# held the flock for as long as the container lived. The next install, upgrade, uninstall or
# --rollback then died with "another install/upgrade/uninstall is running". Reproduced live on
# toypark1234, where /proc named conmon and slirp4netns as the two holders, and worked around here
# with an app_unlocked wrapper that closed the descriptor for one command.
#
# 1.5.0 deletes the descriptor instead. The lock is the DIRECTORY <state>/<app>/lock.d holding an
# owner record of boot id, pid and that pid's start time, and "held" means that owner is still
# alive: a crashed lock is taken over, not waited on. QL_LOCK_FD survives as an always-empty
# variable so that scripts written against <= 1.4.0 still parse under `set -u`, which makes every
# app_unlocked wrapper a no-op. The wrapper is therefore gone, and what follows pins the property
# it was standing in for instead of pinning the wrapper.

t_the_lock_keeps_no_descriptor_for_a_child_to_inherit() {
  local dir rec f n=0
  ql_lock podman-mcp-admin
  eq "${QL_LOCK_FD:-}" '' "QL_LOCK_FD (1.5.0 keeps it only so <= 1.4.0 callers still parse)"
  dir=$(_ql_state_dir podman-mcp-admin)/lock.d
  [[ -d $dir ]] || die_t "ql_lock did not create the lock directory $dir"
  read -r rec <"$dir/owner" || die_t "the lock directory carries no owner record"
  eq "$rec" "$(_ql_owner_record)" "the owner record of the lock this shell holds"
  for f in /proc/self/fd/*; do
    if [[ $(readlink -- "$f" 2>/dev/null) == "$dir"* ]]; then n=$((n + 1)); fi
  done
  eq "$n" 0 "descriptors in this shell pointing at the lock"
  # ... and the same seen from an ordinary child, which is what conmon is
  # shellcheck disable=SC2016 # the child shell expands these, not this one
  eq "$(bash -c 'n=0
    for f in /proc/self/fd/*; do
      if [[ $(readlink -- "$f" 2>/dev/null) == "$1"* ]]; then n=$((n + 1)); fi
    done
    printf %s "$n"' _ "$dir")" 0 "lock descriptors an ordinary child inherits"
  return 0
}

t_a_rollback_can_take_the_lock_after_an_earlier_run_left_processes_behind() {
  # The user-visible property app_unlocked was faking. A run takes the lock, leaves a process
  # running that outlives it - conmon's stand-in, and the reason the old descriptor leaked - and
  # exits normally. The rollback is this shell, and it must be able to take the same lock while
  # that process is still alive. Under 1.4.0 it could not: the flock was still held.
  local lingerer
  # shellcheck disable=SC2016 # the child shell expands these, not this one
  bash -c '
    . "$1/scripts/lib/quadlet-lib.sh"
    ql_lock podman-mcp-admin
    sleep 30 &
    printf "%s" "$!" >"$2"
  ' _ "$REPO" "$T/lingerer.pid" || die_t "the first run could not take the lock"
  lingerer=$(cat "$T/lingerer.pid")
  kill -0 "$lingerer" 2>/dev/null || die_t "the fixture is wrong: the lingering child is already gone"
  ql_lock podman-mcp-admin
  eq "$(head -n1 "$(_ql_state_dir podman-mcp-admin)/lock.d/owner")" "$(_ql_owner_record)" \
    "the owner record after the rollback took the lock"
  kill -0 "$lingerer" 2>/dev/null \
    || die_t "the lingering child died first; the test proved nothing"
  kill -9 "$lingerer" 2>/dev/null || true
  return 0
}

t_a_crashed_run_does_not_block_the_lock_for_ever() {
  # The same thing with no chance to tidy up: SIGKILL, so no EXIT trap runs and the lock directory
  # is left behind while a child of the dead run is still alive. Ownership is decided by the owner
  # record, not by the directory, so the next run takes the lock over and says so.
  local lingerer dir err
  dir=$(_ql_state_dir podman-mcp-admin)/lock.d
  # shellcheck disable=SC2016 # the child shell expands these, not this one
  bash -c '
    . "$1/scripts/lib/quadlet-lib.sh"
    ql_lock podman-mcp-admin
    sleep 30 &
    printf "%s" "$!" >"$2"
    kill -9 $$
  ' _ "$REPO" "$T/crashed.pid" || true
  lingerer=$(cat "$T/crashed.pid" 2>/dev/null) || die_t "the fixture never started the lingering child"
  kill -0 "$lingerer" 2>/dev/null || die_t "the fixture is wrong: the lingering child is already gone"
  [[ -d $dir ]] || die_t "the fixture is wrong: the killed run still tidied its own lock away"
  err=$T/takeover.err
  ql_lock podman-mcp-admin 2>"$err" # a ql_die here would end the test, which is exactly the 1.4.0 behaviour
  has "$(cat "$err")" "taking over the lock left behind by pid" "the takeover warning"
  eq "$(head -n1 "$dir/owner")" "$(_ql_owner_record)" "the owner record after the takeover"
  kill -9 "$lingerer" 2>/dev/null || true
  return 0
}

t_a_second_live_run_is_still_refused() {
  # Dropping app_unlocked must not turn the lock into a no-op. While the owner is ALIVE the lock
  # is still exclusive and a second run dies, rather than cutting over on top of the first.
  local holder dir n=0
  dir=$(_ql_state_dir podman-mcp-admin)/lock.d
  # shellcheck disable=SC2016 # the child shell expands these, not this one
  bash -c '. "$1/scripts/lib/quadlet-lib.sh"; ql_lock podman-mcp-admin; sleep 30' _ "$REPO" &
  holder=$!
  while ((n++ < 200)) && [[ ! -s $dir/owner ]]; do sleep 0.05; done
  [[ -s $dir/owner ]] || { kill -9 "$holder" 2>/dev/null; die_t "the holder never took the lock"; }
  # a fresh process with no QL_LOCK_HELD to inherit: it must be refused outright
  # shellcheck disable=SC2016 # the child shell expands these, not this one
  expect_fail env -u QL_LOCK_HELD bash -c '. "$1/scripts/lib/quadlet-lib.sh"; ql_lock podman-mcp-admin' _ "$REPO"
  has "$OUT" "another install/upgrade/uninstall of podman-mcp-admin is running"
  kill -9 "$holder" 2>/dev/null || true
  return 0
}

t_a_child_script_reuses_the_lock_its_caller_holds() {
  # 1.5.0 exports QL_LOCK_HELD, so a wrapper can hold the lock and still call install.sh: the
  # nested ql_lock keeps the caller's lock instead of dying on it. That is the library contract
  # which replaces every local "skip ql_lock when the wrapper already holds it" hack.
  ql_lock podman-mcp-admin
  [[ -n ${QL_LOCK_HELD:-} ]] || die_t "ql_lock did not publish QL_LOCK_HELD for child scripts"
  # shellcheck disable=SC2016 # the child shell expands these, not this one
  expect_ok bash -c '. "$1/scripts/lib/quadlet-lib.sh"; ql_lock podman-mcp-admin' _ "$REPO"
  has "$OUT" "keeping the lock held by the calling script"
  [[ -d $(_ql_state_dir podman-mcp-admin)/lock.d ]] || die_t "the child released the lock its caller holds"
  return 0
}

t_no_1_4_0_lock_workaround_survives_in_the_scripts() {
  # app_unlocked closed a descriptor 1.5.0 never opens, and WOOW_QL_LOCK_HELD skipped a nested
  # ql_lock the library now resolves itself - and would silently stop install.sh locking at all
  # whenever that variable is stale in the environment. Neither may come back.
  local hit
  hit=$(grep -rnE 'app_unlocked|QL_LOCK_FD|WOOW_QL_LOCK_HELD' "$REPO/scripts" --include='*.sh' \
    | grep -v '/lib/quadlet-lib\.sh:') || true
  [[ -z $hit ]] || die_t "a quadlet-lib 1.4.0 lock workaround is back:"$'\n'"$hit"
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
  # SC2094: a test may cat its own probe log inside the subshell; the redirection below is the
  # harness's, not that file's.
  # shellcheck disable=SC2094
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
