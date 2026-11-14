#!/usr/bin/env bash
# Install the Headscale product as an independent rootless Quadlet stack.
# Usage: install.sh [--port N] [--bind IP] [--origin URL] [--set KEY=VALUE]...
#                   (--release-manifest FILE --source-checkout DIR | --skip-release-signature)
#                   [--no-start] [--dry-run]
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
DEPLOY=$REPO/products/deploy/headscale
# shellcheck source=../../../../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=../../release-gate.sh
. "$REPO/products/deploy/release-gate.sh"

APP=headscale-mcp-admin
CONTAINER=$APP
UNIT=$APP.service
VERSION=$(<"$DEPLOY/VERSION")
ENV_FILE=$HOME/.config/$APP/$APP.env
EXAMPLE=$DEPLOY/config/$APP.env.example
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
SECRETS=("$APP-jwt-secret:random:64" "$APP-password:random:32")

sets=() no_start=0 release_manifest='' source_checkout=''
while (($#)); do
  case $1 in
    --port) sets+=("MCP_ADMIN_PORT=${2:?--port needs a value}"); shift ;;
    --bind) sets+=("MCP_ADMIN_BIND=${2:?--bind needs an address}"); shift ;;
    --origin) sets+=("HEADSCALE_MCP_ALLOWED_ORIGINS=${2:?--origin needs an URL}"); shift ;;
    --set) sets+=("${2:?--set needs KEY=VALUE}"); shift ;;
    --rebuild) ql_die "installer builds are disabled; use a reviewed immutable release image" ;;
    --no-build) : ;;
    --release-manifest) release_manifest=${2:?--release-manifest needs a file}; shift ;;
    --source-checkout) source_checkout=${2:?--source-checkout needs a directory}; shift ;;
    --skip-release-signature) MCP_RELEASE_UNSIGNED=1 ;;
    --no-start) no_start=1 ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    -h | --help) sed -n '2,5p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1" ;;
  esac
  shift
done
export QL_APP=$APP
DRY=${QL_DRY_RUN:-0}

command -v curl >/dev/null 2>&1 || ql_die "curl not found"
command -v python3 >/dev/null 2>&1 || ql_die "python3 not found"
ql_require_rootless
mcp_release_gate "$APP" headscale "$release_manifest" "$source_checkout"
IMAGE=$RELEASE_IMAGE_ID
mcp_release_check_active_collision "$APP" "$UNIT" "$QDIR"
ql_preflight 4.9

WORK=$(mktemp -d "${TMPDIR:-/tmp}/$APP-install.XXXXXX")
ql_cleanup work rm -rf "$WORK"
envsrc=$WORK/$APP.env
if [[ -f $ENV_FILE ]]; then cp -- "$ENV_FILE" "$envsrc"; else cp -- "$EXAMPLE" "$envsrc"; fi
chmod 600 "$envsrc"
setenv() { QL_DRY_RUN=0 ql_env_set "$envsrc" "$1" "$2"; }
for kv in "${sets[@]}"; do
  [[ $kv == *=* ]] || ql_die "--set requires KEY=VALUE"
  grep -q "^${kv%%=*}=" "$EXAMPLE" || ql_die "${kv%%=*} is not a Headscale setting"
  setenv "${kv%%=*}" "${kv#*=}"
done
ql_env_load "$envsrc"
BIND=$(ql_env_get MCP_ADMIN_BIND)
PORT=$(ql_env_get MCP_ADMIN_PORT)
ORIGINS=$(ql_env_get HEADSCALE_MCP_ALLOWED_ORIGINS)
COOKIE=$(ql_env_get HEADSCALE_MCP_SECURE_COOKIE)
CIDRS=$(ql_env_get HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS)
# Env files from before the memory ceiling get the default written into them.
if [[ -z $(ql_env_get MCP_MEMORY_LIMIT '') ]]; then
  setenv MCP_MEMORY_LIMIT 1g
  ql_env_load "$envsrc"
fi
MEMORY=$(ql_env_get MCP_MEMORY_LIMIT)
python3 "$DEPLOY/validate_env.py" --bind "$BIND" --port "$PORT" \
  --origins "$ORIGINS" --secure-cookie "$COOKIE" --cidrs "$CIDRS" --memory "$MEMORY" \
  || ql_die "invalid Headscale deployment settings"
[[ $BIND == 127.0.0.1 ]] || ql_warn "GUI is reachable on $BIND; use a trusted HTTPS reverse proxy"

ql_check_container_collision "$CONTAINER" "$UNIT"
published=$(sed -n 's/^PublishPort=//p' "$QDIR/$CONTAINER.container" 2>/dev/null || true)
if [[ $published != "$BIND:$PORT:8080" || $(systemctl --user is-active "$UNIT" 2>/dev/null || true) != active ]] \
  && command -v ss >/dev/null 2>&1 && [[ -n $(ss -ltnH "sport = :$PORT" 2>/dev/null || true) ]]; then
  ql_die "port $PORT is already in use; choose --port"
fi

mkdir -p "$WORK/out"
ql_render "$DEPLOY/quadlet" "$envsrc" "$DEPLOY/quadlet/render-vars" "$WORK/out" \
  "IMAGE=$IMAGE" "MCP_MEMORY_LIMIT=$MEMORY"
ql_dryrun "$WORK/out" --verify --ref-dir "$QDIR" \
  || ql_die "rendered Headscale units failed Quadlet validation"
for file in "$WORK/out"/*; do
  unit=$(ql_unit_for "$file")
  [[ -z $unit ]] || ql_check_unit_shadow "$unit" "$APP"
done
mcp_release_recheck "$APP" headscale "$release_manifest" "$source_checkout"
ql_enable_linger
ql_env_ensure "$EXAMPLE" "$ENV_FILE"
if [[ $DRY != 1 ]] && ! cmp -s -- "$envsrc" "$ENV_FILE"; then
  install -m 600 -- "$envsrc" "$ENV_FILE" || ql_die "cannot save $ENV_FILE"
fi

ql_pull_images "$WORK/out"
for secret in "${SECRETS[@]}"; do ql_secret_ensure "${secret%%:*}" "${secret#*:}"; done

changed=$(ql_install_files "$WORK/out" "$APP" --prune)
[[ -z $changed ]] || ql_info "changed: $(tr '\n' ' ' <<<"$changed")"
if [[ $DRY == 1 ]]; then ql_info "dry-run complete; no deployment changed"; exit 0; fi
if ((no_start)); then
  systemctl --user daemon-reload
  ql_info "installed; start with systemctl --user start $UNIT"
  exit 0
fi
ql_apply_units "$APP" "$UNIT"
ql_wait_container_healthy "$CONTAINER" 180 \
  || ql_die "$CONTAINER did not become healthy; inspect journalctl --user -u $UNIT"
ql_wait_http "http://$BIND:$PORT/healthz" 200 60 \
  || ql_die "Headscale GUI did not answer /healthz"
"$DEPLOY/scripts/smoke.sh" || ql_die "Headscale smoke check failed"
ql_info "Headscale MCP $VERSION is running at http://$BIND:$PORT/"
ql_info "First-boot password: podman secret inspect --showsecret --format '{{.SecretData}}' $APP-password"
ql_info "Connector URL: $DEPLOY/scripts/show-connector.sh (treat the URL as a credential)"
