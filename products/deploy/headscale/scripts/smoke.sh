#!/usr/bin/env bash
# Read-only smoke: management login, Origin denial and MCP initialization.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
# shellcheck source=../../../../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=headscale-mcp-admin
ENV_FILE=$HOME/.config/$APP/$APP.env
[[ -f $ENV_FILE ]] || ql_die "$ENV_FILE not found"
ql_env_load "$ENV_FILE"
BIND=$(ql_env_get MCP_ADMIN_BIND)
PORT=$(ql_env_get MCP_ADMIN_PORT)
BASE=http://$BIND:$PORT

[[ $(systemctl --user is-active "$APP.service") == active ]] || ql_die "$APP.service is not active"
[[ $(podman inspect --format '{{.State.Health.Status}}' "$APP") == healthy ]] \
  || ql_die "$APP is not healthy"
[[ $(curl -s -o /dev/null -w '%{http_code}' -m 10 "$BASE/healthz") == 200 ]] \
  || ql_die "/healthz did not return 200"
[[ $(podman exec "$APP" python -c 'import os; print(oct(os.stat("/data/config.json").st_mode & 0o777))') == 0o600 ]] \
  || ql_die "config.json is not private"

# Build login JSON inside the container. Only the HTTP response code is displayed.
login_code=$(podman exec "$APP" python -c \
  'import json; c=json.load(open("/data/config.json")); print(json.dumps({"password":c["admin_password"]}))' \
  | curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Content-Type: application/json' \
      --data @- "$BASE/api/auth/login")
[[ $login_code == 200 ]] || ql_die "management login failed"
[[ $(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Content-Type: application/json' \
      --data '{"password":"incorrect-smoke-password"}' "$BASE/api/auth/login") == 401 ]] \
  || ql_die "incorrect management password was not rejected"

token=$(podman exec "$APP" python -c \
  'import json; print(json.load(open("/data/config.json")).get("mcp_auth_token", ""))')
[[ -n $token ]] || ql_die "MCP token was not issued on first boot"
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"headscale-podman-smoke","version":"1"}}}'
mcp_code() {
  local candidate=$1 origin=${2:-}
  local -a headers=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
  [[ -z $origin ]] || headers+=(-H "Origin: $origin")
  printf 'url = "%s/private_%s/mcp"\n' "$BASE" "$candidate" \
    | curl -s -o /dev/null -w '%{http_code}' -m 15 -K - -X POST \
        "${headers[@]}" --data "$init"
}
[[ $(mcp_code "$token") == 200 ]] || ql_die "MCP initialize failed"
[[ $(mcp_code invalid-smoke-token) == 403 ]] || ql_die "wrong MCP token was not rejected"
[[ $(mcp_code "$token" https://untrusted.example) == 403 ]] \
  || ql_die "untrusted Origin was not rejected"
token=''
ql_info "Headscale management, token and Origin smoke checks passed"
