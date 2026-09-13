#!/usr/bin/env bash
# scripts/install.sh: install or update podman-mcp-admin as rootless Quadlet units
# (podman >= 4.9, systemd --user, linger). Idempotent: an unchanged re-run restarts nothing.
#
#   scripts/install.sh [--port N] [--bind ADDR] [--set KEY=VALUE]... [--rebuild | --no-build]
#                      [--no-start] [--dry-run] [--yes]
#
#   --port N          publish port (MCP_ADMIN_PORT, default 8080); saved in the env file
#   --bind ADDR       publish address (MCP_ADMIN_BIND, default 127.0.0.1); saved in the env file
#   --set KEY=VALUE   set any key of config/podman-mcp-admin.env.example in the env file
#   --rebuild         rebuild the image even when the VERSION tag already exists
#   --no-build        never build; the image tag must already exist
#   --no-start        install the files and daemon-reload only
#   --dry-run         render, validate and report what would change; change nothing
#   --yes             accepted for symmetry with the other scripts (nothing to confirm)
#
# Per-host values live in ~/.config/podman-mcp-admin/podman-mcp-admin.env (0600), created
# from config/podman-mcp-admin.env.example on the first run. Secrets are podman secrets.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"

# ---- per-repo settings -----------------------------------------------------------------
APP=podman-mcp-admin
ENV_FILE=$HOME/.config/$APP/$APP.env
EXAMPLE=$REPO/config/$APP.env.example
PODMAN_MIN=4.9
VERSION=$(<"$REPO/VERSION")
IMAGE=localhost/woow-podman-mcp-admin:$VERSION
CONTAINER=podman-mcp-admin
UNIT=podman-mcp-admin.service
# name:mode for ql_secret_ensure; created once, never overwritten by install.sh
SECRETS=(
  podman-mcp-admin-jwt-secret:random:64
  podman-mcp-admin-password:random:24
  podman-mcp-admin-mcp-token:random:43
)
# hand-written units of the pre-Quadlet deployment (README "Migrating an existing deployment")
LEGACY_UNITS=(podman-podman-mcp-admin.service container-podman-mcp-admin.service)
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
# ------------------------------------------------------------------------------------------

usage() { sed -n '2,19p' "$0"; }
sets=() build=auto no_start=0
while (($#)); do
  case $1 in
    --port) sets+=("MCP_ADMIN_PORT=${2:?--port needs a value}"); shift ;;
    --bind) sets+=("MCP_ADMIN_BIND=${2:?--bind needs a value}"); shift ;;
    --set) sets+=("${2:?--set needs KEY=VALUE}"); shift ;;
    --rebuild) build=always ;;
    --no-build) build=never ;;
    --no-start) no_start=1 ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    --yes) ;;
    -h | --help) usage; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
export QL_APP=$APP
DRY=${QL_DRY_RUN:-0}

# ---- 1. host preflight ---------------------------------------------------------------------
ql_preflight "$PODMAN_MIN"
command -v curl >/dev/null 2>&1 || ql_die "curl not found (sudo apt-get install curl)"
ql_enable_linger
ql_lock "$APP"
ql_enable_podman_socket

WORK=$(mktemp -d "${TMPDIR:-/tmp}/$APP-install.XXXXXX")
ql_cleanup work rm -rf "$WORK"

# ---- 2. per-host settings (D2: rendered from the env file at install time) -----------------
ql_env_ensure "$EXAMPLE" "$ENV_FILE"
# Settings are staged in a private copy and saved only after validation, so a rejected
# --port/--bind/--set never lands in the env file. A dry run never saves them.
envsrc=$WORK/$APP.env
if [[ -f $ENV_FILE ]]; then cp -- "$ENV_FILE" "$envsrc"; else cp -- "$EXAMPLE" "$envsrc"; QL_ENV_CREATED=1; fi
chmod 600 "$envsrc"
setenv() { QL_DRY_RUN=0 ql_env_set "$envsrc" "$1" "$2"; }
if [[ $QL_ENV_CREATED == 1 ]]; then
  api=$(podman version --format '{{.Server.APIVersion}}' 2>/dev/null || true)
  if [[ $api =~ ^v?([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    setenv PODMAN_API_VERSION "v${BASH_REMATCH[1]}"
  else
    ql_warn "could not read the podman API version; keeping PODMAN_API_VERSION from the example"
  fi
  ql_info "created $ENV_FILE with defaults; edit it and re-run to change them"
fi
for kv in "${sets[@]}"; do
  [[ $kv == *=* ]] || ql_die "--set wants KEY=VALUE, got '$kv'"
  grep -q "^${kv%%=*}=" "$EXAMPLE" || ql_die "--set: ${kv%%=*} is not a setting of ${EXAMPLE##*/}"
  setenv "${kv%%=*}" "${kv#*=}"
done
ql_env_load "$envsrc"

BIND=$(ql_env_get MCP_ADMIN_BIND)
PORT=$(ql_env_get MCP_ADMIN_PORT)
ql_assert_match MCP_ADMIN_BIND "$BIND" '(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}'
[[ $BIND != 0.0.0.0 ]] || ql_die "MCP_ADMIN_BIND=0.0.0.0 would publish the podman socket to every network; use 127.0.0.1 or one LAN IP"
ql_assert_match MCP_ADMIN_PORT "$PORT" '[1-9][0-9]{0,4}'
((PORT <= 65535)) || ql_die "MCP_ADMIN_PORT=$PORT is not a TCP port"
[[ $BIND == 127.0.0.1 ]] || ql_warn "MCP_ADMIN_BIND=$BIND: the console is reachable from that network, not only from this host"
ql_assert_match PODMAN_MCP_PROFILE "$(ql_env_get PODMAN_MCP_PROFILE)" 'readonly|safe|full'
ql_assert_match PODMAN_API_VERSION "$(ql_env_get PODMAN_API_VERSION)" 'v[0-9]+\.[0-9]+\.[0-9]+'
ql_assert_match PODMAN_MCP_MAX_CHARS "$(ql_env_get PODMAN_MCP_MAX_CHARS)" '[1-9][0-9]*'
ql_assert_match JWT_EXPIRY_HOURS "$(ql_env_get JWT_EXPIRY_HOURS)" '[1-9][0-9]*'
# Quadlet splits Environment= on blanks and systemd would expand "$NAME": keep the regex simple.
ql_assert_match PODMAN_MCP_NAME_ALLOW "$(ql_env_get PODMAN_MCP_NAME_ALLOW)" '[^[:space:]"'"'"'\\]*'

# ---- 3. legacy guards ------------------------------------------------------------------------
for u in "${LEGACY_UNITS[@]}"; do
  if systemctl --user is-active --quiet "$u" 2>/dev/null || [[ $(systemctl --user is-enabled "$u" 2>/dev/null || true) == enabled ]]; then
    ql_die "legacy unit $u is still enabled or running; follow README \"Migrating an existing deployment\" first"
  fi
done
ql_check_container_collision "$CONTAINER" "$UNIT"
# The port must be free, unless our running container is the one already publishing it.
published=$(sed -n 's/^PublishPort=//p' "$QDIR/$CONTAINER.container" 2>/dev/null || true)
if [[ $published != "$BIND:$PORT:8080" || $(systemctl --user is-active "$UNIT" 2>/dev/null || true) != active ]] \
  && command -v ss >/dev/null 2>&1 && [[ -n $(ss -ltnH "sport = :$PORT" 2>/dev/null || true) ]]; then
  ql_die "port $PORT is already in use on this host (ss -ltnp 'sport = :$PORT'); pick another with --port"
fi

# ---- 4. render the units and validate them against the podman 4.9.3 generator -------------
mkdir -p "$WORK/src" "$WORK/out"
cp -p "$REPO"/quadlet/*.container "$REPO"/quadlet/*.volume "$REPO"/quadlet/*.network "$WORK/src/"
ql_render "$WORK/src" "$envsrc" "$REPO/quadlet/render-vars" "$WORK/out"
ql_dryrun "$WORK/out" --verify --ref-dir "$QDIR" || ql_die "the rendered units failed the dry-run; nothing was installed"
for f in "$WORK/out"/*; do
  u=$(ql_unit_for "$f")
  [[ -z $u ]] || ql_check_unit_shadow "$u" "$APP"
done

# Every check passed: only now do new --port/--bind/--set values reach the env file.
if [[ $DRY != 1 ]] && ! cmp -s -- "$envsrc" "$ENV_FILE"; then
  install -m 600 -- "$envsrc" "$ENV_FILE" || ql_die "cannot update $ENV_FILE"
  ql_info "saved the new settings in $ENV_FILE"
fi

# ---- 5. image and secrets, before any unit changes -------------------------------------------
built=0
if [[ $build == always ]] || ! podman image exists "$IMAGE"; then
  [[ $build != never ]] || ql_die "image $IMAGE does not exist and --no-build was given"
  if [[ $DRY == 1 ]]; then
    ql_info "[dry-run] would build $IMAGE"
  else
    ql_info "building $IMAGE (podman build --format docker; a few minutes on a small host)"
    podman build --format docker -t "$IMAGE" -f "$REPO/Dockerfile" "$REPO" || ql_die "podman build failed; nothing was changed"
    built=1
  fi
fi
if podman image exists "$IMAGE"; then ql_pull_images "$WORK/out"; fi
for s in "${SECRETS[@]}"; do ql_secret_ensure "${s%%:*}" "${s#*:}"; done

# ---- 6. install changed files, then start / restart only what changed -----------------------
changed=$(ql_install_files "$WORK/out" "$APP" --prune)
[[ -z $changed ]] || ql_info "changed: $(tr '\n' ' ' <<<"$changed")"
if [[ $DRY == 1 ]]; then
  ql_info "dry-run complete; nothing was changed"
  exit 0
fi
((built == 0)) || ql_mark_changed "$APP" "$UNIT"
if ((no_start)); then
  systemctl --user daemon-reload
  ql_info "installed; not started (--no-start). Start with: systemctl --user start $UNIT"
  exit 0
fi
ql_apply_units "$APP" "$UNIT"

# ---- 7. health and smoke -----------------------------------------------------------------------
ql_wait_container_healthy "$CONTAINER" 180 \
  || ql_die "$CONTAINER did not become healthy; see: journalctl --user -u $UNIT -n 100"
ql_wait_http "http://$BIND:$PORT/healthz" '200' 60 || ql_die "http://$BIND:$PORT/healthz did not answer 200"
"$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed; see the output above"

cat >&2 <<EOF

$APP $VERSION is installed and healthy.

  Console      http://$BIND:$PORT/   (loopback: put a tunnel or proxy on this host in front)
  Password     podman secret inspect --showsecret --format '{{.SecretData}}' podman-mcp-admin-password
               (first-boot seed; after a password change in the GUI, /data/config.json is authoritative)
  Connector    $REPO/scripts/show-connector.sh   (the URL is a bearer credential: treat it as a password)
  Profile      $(ql_env_get PODMAN_MCP_PROFILE): the token holder gets this user's podman at that level (README "Authentication")
  Logs         journalctl --user -u $UNIT -f
  Settings     $ENV_FILE (edit, then re-run $0)

EOF
