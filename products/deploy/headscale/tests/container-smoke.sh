#!/usr/bin/env bash
# Disposable rootless Podman smoke: first boot, GUI, MCP, restart persistence, revoked restore.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
# shellcheck source=../../shared/restore-volume.sh
. "$REPO/products/deploy/shared/restore-volume.sh"
IMAGE=${1:?usage: container-smoke.sh IMAGE}
suffix="${GITHUB_RUN_ID:-local}-$$"
C="headscale-smoke-$suffix"
V="headscale-smoke-data-$suffix"
FIRST=$V
R="headscale-smoke-restored-$suffix"
WORK=$(mktemp -d)
PASS="headscale-smoke-password-$suffix"
JWT="headscale-smoke-jwt-$suffix"
PORT=18991
cleanup() {
  podman rm -f "$C" >/dev/null 2>&1 || true
  podman volume rm "$FIRST" "$R" >/dev/null 2>&1 || true
  podman secret rm "$PASS" "$JWT" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
PASS_VALUE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
printf '%s' "$PASS_VALUE" | podman secret create "$PASS" - >/dev/null
python3 -c 'import secrets; print(secrets.token_urlsafe(64))' | podman secret create "$JWT" - >/dev/null
podman volume create "$V" >/dev/null

start() {
  podman run -d --name "$C" --read-only --cap-drop ALL --security-opt no-new-privileges \
    --memory 1g -p "127.0.0.1:$PORT:8080" -v "$V:/data:U" \
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
# The core sizes extra profiles from this file; the Quadlet sets the same --memory.
[[ $(podman exec "$C" cat /sys/fs/cgroup/memory.max) == 1073741824 ]]
[[ $(podman exec "$C" python -c 'import os; print(oct(os.stat("/data/config.json").st_mode & 0o777))') == 0o600 ]]
python3 "$REPO/products/deploy/headscale/tests/container-smoke.py" "$C" "$PORT"
before=$(podman exec "$C" sha256sum /data/config.json | cut -d' ' -f1)
podman rm -f "$C" >/dev/null
start
python3 "$REPO/products/deploy/headscale/tests/container-smoke.py" "$C" "$PORT"
after=$(podman exec "$C" sha256sum /data/config.json | cut -d' ' -f1)
[[ $after == "$before" ]]

# Revoked restore of the exported volume, the way scripts/restore.sh does it.
old_token=$(podman exec "$C" python -c 'import json; print(json.load(open("/data/config.json"))["mcp_auth_token"])')
podman rm -f "$C" >/dev/null
podman volume export "$V" -o "$WORK/backup.tar"
podman volume create "$R" >/dev/null
mcp_restore_revoked "$R" "$WORK/backup.tar" "$IMAGE" "$PASS" headscale
V=$R
start
EXPECTED=$PASS_VALUE python3 - "$C" <<'PY'
import json, os, subprocess, sys
def read(name):
    return json.loads(subprocess.check_output(["podman", "exec", sys.argv[1], "cat", f"/data/{name}"], text=True))
root = read("config.json")
assert root["admin_password"] == os.environ["EXPECTED"], "GUI password is not the installed secret"
assert root.get("profile_index"), "profiles were not restored"
for name in ["config.json", *(f"profiles/{p}/config.json" for p in root["profile_index"])]:
    config = read(name)
    assert config.get("mcp_auth_revoked") is True and not config.get("mcp_auth_token"), "a token survived"
PY
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"headscale-restore-ci","version":"1"}}}'
code=$(printf 'url = "http://127.0.0.1:%s/private_%s/mcp"\n' "$PORT" "$old_token" \
  | curl -s -o /dev/null -w '%{http_code}' -m 15 -K - -X POST -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' --data "$init")
[[ $code == 403 ]]
printf 'Headscale container boot, GUI setup, authentication, restart persistence and revoked restore passed\n'
