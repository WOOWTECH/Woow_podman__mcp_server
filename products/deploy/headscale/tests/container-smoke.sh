#!/usr/bin/env bash
# Disposable rootless Podman smoke: first boot, GUI, MCP, restart persistence.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
IMAGE=${1:?usage: container-smoke.sh IMAGE}
suffix="${GITHUB_RUN_ID:-local}-$$"
C="headscale-smoke-$suffix"
V="headscale-smoke-data-$suffix"
PASS="headscale-smoke-password-$suffix"
JWT="headscale-smoke-jwt-$suffix"
PORT=18991
cleanup() {
  podman rm -f "$C" >/dev/null 2>&1 || true
  podman volume rm "$V" >/dev/null 2>&1 || true
  podman secret rm "$PASS" "$JWT" >/dev/null 2>&1 || true
}
trap cleanup EXIT
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' | podman secret create "$PASS" - >/dev/null
python3 -c 'import secrets; print(secrets.token_urlsafe(64))' | podman secret create "$JWT" - >/dev/null
podman volume create "$V" >/dev/null

start() {
  podman run -d --name "$C" --read-only --cap-drop ALL --security-opt no-new-privileges \
    -p "127.0.0.1:$PORT:8080" -v "$V:/data:U" \
    --secret "$PASS,type=env,target=ADMIN_PASSWORD" \
    --secret "$JWT,type=env,target=JWT_SECRET" \
    -e "HEADSCALE_MCP_ALLOWED_ORIGINS=http://127.0.0.1:$PORT" \
    -e HEADSCALE_MCP_SECURE_COOKIE=false "$IMAGE" >/dev/null
  for _ in $(seq 1 60); do
    [[ $(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:$PORT/healthz" || true) == 200 ]] && return 0
    sleep 1
  done
  podman logs "$C" | tail -60 >&2
  return 1
}

start
[[ $(podman exec "$C" python -c 'import os; print(oct(os.stat("/data/config.json").st_mode & 0o777))') == 0o600 ]]
python3 "$REPO/products/deploy/headscale/tests/container-smoke.py" "$C" "$PORT"
before=$(podman exec "$C" sha256sum /data/config.json | cut -d' ' -f1)
podman rm -f "$C" >/dev/null
start
python3 "$REPO/products/deploy/headscale/tests/container-smoke.py" "$C" "$PORT"
after=$(podman exec "$C" sha256sum /data/config.json | cut -d' ' -f1)
[[ $after == "$before" ]]
printf 'Headscale container boot, GUI setup, authentication and restart persistence passed\n'
