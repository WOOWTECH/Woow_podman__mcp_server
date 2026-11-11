#!/usr/bin/env bash
# Disposable first boot, MCP authentication and volume persistence for an app image.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
PRODUCT=${1:?usage: container-smoke.sh PRODUCT IMAGE}
IMAGE=${2:?usage: container-smoke.sh PRODUCT IMAGE}
suffix="${GITHUB_RUN_ID:-local}-$$"
container="$PRODUCT-mcp-smoke-$suffix"
volume="${PRODUCT}_mcp_smoke_data_$suffix"
password="$PRODUCT-mcp-smoke-password-$suffix"
jwt="$PRODUCT-mcp-smoke-jwt-$suffix"
port=18990

cleanup() {
  podman rm -f "$container" >/dev/null 2>&1 || true
  podman volume rm "$volume" >/dev/null 2>&1 || true
  podman secret rm "$password" "$jwt" >/dev/null 2>&1 || true
}
trap cleanup EXIT
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' | podman secret create "$password" - >/dev/null
python3 -c 'import secrets; print(secrets.token_urlsafe(64))' | podman secret create "$jwt" - >/dev/null
podman volume create "$volume" >/dev/null

start() {
  podman run -d --name "$container" --read-only --cap-drop ALL \
    --security-opt no-new-privileges -p "127.0.0.1:$port:8080" \
    -v "$volume:/data:U" \
    --secret "$password,type=env,target=ADMIN_PASSWORD" \
    --secret "$jwt,type=env,target=JWT_SECRET" \
    -e MCP_ADMIN_CONFIG=/data/config.json "$IMAGE" >/dev/null
  for _ in $(seq 1 90); do
    [[ $(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:$port/healthz" || true) == 200 ]] && return 0
    sleep 1
  done
  podman logs "$container" | tail -60 >&2
  return 1
}

start
[[ $(podman exec "$container" python -c 'import os; print(oct(os.stat("/data/config.json").st_mode & 0o777))') == 0o600 ]]
python3 "$REPO/products/deploy/shared/smoke.py" "$container" "http://127.0.0.1:$port"
before=$(podman exec "$container" sha256sum /data/config.json | cut -d' ' -f1)
podman rm -f "$container" >/dev/null
start
python3 "$REPO/products/deploy/shared/smoke.py" "$container" "http://127.0.0.1:$port"
after=$(podman exec "$container" sha256sum /data/config.json | cut -d' ' -f1)
[[ $after == "$before" ]]
printf '%s image boot, GUI, MCP and restart persistence passed\n' "$PRODUCT"
