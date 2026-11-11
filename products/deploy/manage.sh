#!/usr/bin/env bash
# One rootless Quadlet interface for every PaaS-sourced MCP product.
# Usage: manage.sh list | PRODUCT {install|status|smoke|backup|restore|upgrade|uninstall} [options]
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
DEPLOY=$REPO/products/deploy
# shellcheck source=../../../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"

if [[ ${1:-} == list ]]; then
  python3 "$DEPLOY/shared/catalog.py" list
  exit 0
fi
PRODUCT=${1:-}
ACTION=${2:-}
[[ -n $PRODUCT && -n $ACTION ]] || ql_die "usage: $0 list | PRODUCT {install|status|smoke|backup|restore|upgrade|uninstall}"
shift 2
mapfile -d '' -t META < <(python3 "$DEPLOY/shared/catalog.py" get "$PRODUCT")
((${#META[@]} == 5)) || ql_die "unknown product: $PRODUCT"
PACKAGE=${META[0]}
MODULE=${META[1]}
DEFAULT_PORT=${META[2]}
VERSION=${META[3]}
INSTALLER=${META[4]}
APP=$PRODUCT-mcp-admin
UNIT=$APP.service
IMAGE=localhost/woow-$PRODUCT-mcp-admin:$VERSION
VOLUME=${APP//-/_}_data
ENV_FILE=$HOME/.config/$APP/$APP.env
QDIR=${QL_QUADLET_DIR:-$HOME/.config/containers/systemd}
export QL_APP=$APP

if [[ $INSTALLER == headscale ]]; then
  case $ACTION in
    install|smoke|backup|restore|upgrade|uninstall)
      exec "$DEPLOY/headscale/scripts/$ACTION.sh" "$@" ;;
    status)
      [[ -f $ENV_FILE ]] || ql_die "$ENV_FILE not found"
      ql_env_load "$ENV_FILE"
      BIND=$(ql_env_get MCP_ADMIN_BIND)
      PORT=$(ql_env_get MCP_ADMIN_PORT)
      systemctl --user status "$UNIT" --no-pager
      exec curl -fsS "http://$BIND:$PORT/healthz" ;;
    *) ql_die "unknown action $ACTION" ;;
  esac
fi

read_settings() {
  [[ -f $ENV_FILE ]] || ql_die "$ENV_FILE not found; run '$0 $PRODUCT install' first"
  ql_env_load "$ENV_FILE"
  BIND=$(ql_env_get MCP_ADMIN_BIND)
  PORT=$(ql_env_get MCP_ADMIN_PORT)
  python3 "$DEPLOY/shared/validate_env.py" "$BIND" "$PORT" || ql_die "invalid bind settings"
}

case $ACTION in
  install)
    sets=() build=auto no_start=0
    while (($#)); do
      case $1 in
        --port) sets+=("MCP_ADMIN_PORT=${2:?--port needs a value}"); shift ;;
        --bind) sets+=("MCP_ADMIN_BIND=${2:?--bind needs an address}"); shift ;;
        --backend-network) sets+=("MCP_BACKEND_NETWORK=${2:?--backend-network needs a network name}"); shift ;;
        --ssrf-mode) sets+=("WEBHOOK_SECURITY_MODE=${2:?--ssrf-mode needs strict, moderate or permissive}"); shift ;;
        --rebuild) build=always ;;
        --no-build) build=never ;;
        --no-start) no_start=1 ;;
        --dry-run) export QL_DRY_RUN=1 ;;
        *) ql_die "unknown install option $1" ;;
      esac
      shift
    done
    DRY=${QL_DRY_RUN:-0}
    ql_preflight 4.9
    command -v curl >/dev/null || ql_die "curl not found"
    command -v python3 >/dev/null || ql_die "python3 not found"
    ql_enable_linger
    ql_lock "$APP"
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/$APP-install.XXXXXX")
    ql_cleanup work rm -rf "$WORK"
    printf 'MCP_ADMIN_BIND=127.0.0.1\nMCP_ADMIN_PORT=%s\nMCP_BACKEND_NETWORK=\nWEBHOOK_SECURITY_MODE=strict\n' "$DEFAULT_PORT" >"$WORK/example.env"
    ql_env_ensure "$WORK/example.env" "$ENV_FILE"
    envsrc=$WORK/$APP.env
    if [[ -f $ENV_FILE ]]; then cp -- "$ENV_FILE" "$envsrc"; else cp -- "$WORK/example.env" "$envsrc"; fi
    chmod 600 "$envsrc"
    for kv in "${sets[@]}"; do
      QL_DRY_RUN=0 ql_env_set "$envsrc" "${kv%%=*}" "${kv#*=}"
    done
    ql_env_load "$envsrc"
    BIND=$(ql_env_get MCP_ADMIN_BIND)
    PORT=$(ql_env_get MCP_ADMIN_PORT)
    BACKEND_NETWORK=$(ql_env_get MCP_BACKEND_NETWORK '')
    SSRF_MODE=$(ql_env_get WEBHOOK_SECURITY_MODE strict)
    python3 "$DEPLOY/shared/validate_env.py" "$BIND" "$PORT" || ql_die "invalid bind settings"
    [[ $SSRF_MODE == strict || $SSRF_MODE == moderate || $SSRF_MODE == permissive ]] \
      || ql_die "invalid SSRF mode: use strict, moderate or permissive"
    if [[ -n $BACKEND_NETWORK ]]; then
      [[ $BACKEND_NETWORK =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || ql_die "invalid backend network name"
      [[ $BACKEND_NETWORK != "$APP-net" ]] || ql_die "backend network is already attached"
      podman network exists "$BACKEND_NETWORK" || ql_die "backend network $BACKEND_NETWORK does not exist"
    fi
    [[ $BIND == 127.0.0.1 ]] || ql_warn "GUI is reachable on $BIND; use a trusted HTTPS reverse proxy"
    ql_check_container_collision "$APP" "$UNIT"
    published=$(sed -n 's/^PublishPort=//p' "$QDIR/$APP.container" 2>/dev/null || true)
    if [[ $published != "$BIND:$PORT:8080" || $(systemctl --user is-active "$UNIT" 2>/dev/null || true) != active ]] \
      && command -v ss >/dev/null && [[ -n $(ss -ltnH "sport = :$PORT" 2>/dev/null || true) ]]; then
      ql_die "port $PORT is already in use; choose --port"
    fi
    mkdir -p "$WORK/src" "$WORK/out"
    for ext in container volume network; do
      cp -- "$DEPLOY/shared/quadlet/product.$ext" "$WORK/src/$APP.$ext"
    done
    ql_render "$WORK/src" "$envsrc" "$DEPLOY/shared/quadlet/render-vars" "$WORK/out" \
      "PRODUCT=$PRODUCT" "APP=$APP" "IMAGE=$IMAGE" "VOLUME=$VOLUME" "ENV_FILE=$ENV_FILE"
    if [[ -n $BACKEND_NETWORK ]]; then
      sed -i "/^Network=$APP\\.network$/a Network=$BACKEND_NETWORK" "$WORK/out/$APP.container"
    fi
    ql_dryrun "$WORK/out" --verify --ref-dir "$QDIR" || ql_die "Quadlet validation failed"
    for file in "$WORK/out"/*; do
      unit=$(ql_unit_for "$file")
      [[ -z $unit ]] || ql_check_unit_shadow "$unit" "$APP"
    done
    env_changed=0
    if [[ $DRY != 1 ]] && ! cmp -s -- "$envsrc" "$ENV_FILE"; then
      install -m 600 -- "$envsrc" "$ENV_FILE"
      env_changed=1
    fi
    built=0
    if [[ $build == always ]] || ! podman image exists "$IMAGE"; then
      [[ $build != never ]] || ql_die "image $IMAGE missing and --no-build set"
      if [[ $DRY == 1 ]]; then
        ql_info "[dry-run] would build $IMAGE"
      else
        podman build --format docker -t "$IMAGE" -f "$REPO/products/apps/$PRODUCT/Dockerfile" \
          "$REPO/products" || ql_die "image build failed; units were not changed"
        built=1
      fi
    fi
    if podman image exists "$IMAGE"; then ql_pull_images "$WORK/out"; fi
    ql_secret_ensure "$APP-jwt-secret" random:64
    ql_secret_ensure "$APP-password" random:32
    changed=$(ql_install_files "$WORK/out" "$APP" --prune)
    [[ -z $changed ]] || ql_info "changed: $(tr '\n' ' ' <<<"$changed")"
    if [[ $DRY == 1 ]]; then ql_info "dry-run complete; no deployment changed"; exit 0; fi
    ((built == 0 && env_changed == 0)) || ql_mark_changed "$APP" "$UNIT"
    if ((no_start)); then
      systemctl --user daemon-reload
      ql_info "installed; start with systemctl --user start $UNIT"
      exit 0
    fi
    ql_apply_units "$APP" "$UNIT"
    ql_wait_container_healthy "$APP" 180 || ql_die "$APP did not become healthy"
    "$0" "$PRODUCT" smoke
    ql_info "$PRODUCT MCP is running at http://$BIND:$PORT/"
    ;;
  status)
    read_settings
    systemctl --user status "$UNIT" --no-pager
    curl -fsS "http://$BIND:$PORT/healthz"
    ;;
  smoke)
    read_settings
    python3 "$DEPLOY/shared/smoke.py" "$APP" "http://$BIND:$PORT"
    ;;
  backup|restore|upgrade|uninstall)
    exec "$DEPLOY/shared/lifecycle.sh" "$PRODUCT" "$ACTION" "$@"
    ;;
  *) ql_die "unknown action $ACTION" ;;
esac
