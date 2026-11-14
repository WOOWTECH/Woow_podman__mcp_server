#!/usr/bin/env bash
# Disposable first boot, MCP authentication, profiles, volume persistence and a revoked restore
# for an app image. Everything it creates is removed on exit; it never touches an installed unit.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
TESTS=$REPO/products/deploy/shared/tests
# shellcheck source=../restore-volume.sh
. "$REPO/products/deploy/shared/restore-volume.sh"
PRODUCT=${1:?usage: container-smoke.sh PRODUCT IMAGE}
IMAGE=${2:?usage: container-smoke.sh PRODUCT IMAGE}
suffix="${GITHUB_RUN_ID:-local}-$$"
container="$PRODUCT-mcp-smoke-$suffix"
first_volume="${PRODUCT}_mcp_smoke_data_$suffix"
restored_volume="${PRODUCT}_mcp_smoke_restored_$suffix"
prepared_volume="${PRODUCT}_mcp_smoke_prepared_$suffix"
small_volume="${PRODUCT}_mcp_smoke_small_$suffix"
volume=$first_volume
password="$PRODUCT-mcp-smoke-password-$suffix"
jwt="$PRODUCT-mcp-smoke-jwt-$suffix"
port=18990
memory=1g
work=$(mktemp -d)

cleanup() {
  podman rm -f "$container" >/dev/null 2>&1 || true
  podman volume rm -f "$first_volume" "$restored_volume" "$small_volume" "$prepared_volume" \
    "${prepared_volume}_source" >/dev/null 2>&1 || true
  podman secret rm "$password" "$jwt" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
password_value=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
printf '%s' "$password_value" | podman secret create "$password" - >/dev/null
python3 -c 'import secrets; print(secrets.token_urlsafe(64))' | podman secret create "$jwt" - >/dev/null
podman volume create "$volume" >/dev/null

start() {
  podman run -d --name "$container" --read-only --cap-drop ALL \
    --security-opt no-new-privileges --memory "$memory" -p "127.0.0.1:$port:8080" \
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
smoke() { python3 "$REPO/products/deploy/shared/smoke.py" "$container" "http://127.0.0.1:$port"; }
profiles() { python3 "$TESTS/profile-smoke.py" "$@" "$container" "http://127.0.0.1:$port" "$work/profiles.json"; }

start
# The core sizes extra profiles from this file; the Quadlet sets the same --memory.
[[ $(podman exec "$container" cat /sys/fs/cgroup/memory.max) == 1073741824 ]]
[[ $(podman exec "$container" python -c 'import os; print(oct(os.stat("/data/config.json").st_mode & 0o777))') == 0o600 ]]
smoke
profiles setup
before=$(podman exec "$container" sha256sum /data/config.json | cut -d' ' -f1)
podman rm -f "$container" >/dev/null
start
smoke
after=$(podman exec "$container" sha256sum /data/config.json | cut -d' ' -f1)
[[ $after == "$before" ]]

# Restore the exported volume the way manage.sh restore does, into a fresh volume.
podman rm -f "$container" >/dev/null
podman volume export "$volume" -o "$work/backup.tar"
# A backup restore_guard refuses must fail before anything is replaced, leaving no volume behind.
printf 'not a volume export\n' >"$work/broken.tar"
if mcp_prepare_revoked "$work/broken.tar" "$IMAGE" "$password" "$PRODUCT" "$prepared_volume" 2>/dev/null \
    || podman volume exists "$prepared_volume"; then
  echo "a broken backup was accepted or left a prepared volume" >&2
  exit 1
fi
mcp_prepare_revoked "$work/backup.tar" "$IMAGE" "$password" "$PRODUCT" "$prepared_volume"
podman volume create "$restored_volume" >/dev/null
mcp_publish_prepared "$prepared_volume" "$restored_volume"
if podman volume exists "$prepared_volume"; then echo "prepared volume was not removed" >&2; exit 1; fi
volume=$restored_volume
start
smoke
SMOKE_ADMIN_PASSWORD=$password_value profiles restored
podman rm -f "$container" >/dev/null

if [[ $PRODUCT == n8n ]]; then
  # n8n needs 300 MiB per child plus a 256 MiB reserve: 512 MiB leaves no room for a second profile.
  memory=512m volume=$small_volume
  podman volume create "$volume" >/dev/null
  start
  python3 "$TESTS/profile-smoke.py" capacity "$container" "http://127.0.0.1:$port"
fi
printf '%s image boot, GUI, MCP, profiles, restart persistence and revoked restore passed\n' "$PRODUCT"
