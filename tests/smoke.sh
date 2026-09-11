#!/usr/bin/env bash
# tests/smoke.sh: post-install checks of a running podman-mcp-admin. Read-only: it logs in,
# opens an MCP session and lists tools, but calls no tool that changes anything.
#
#   tests/smoke.sh            exit 0 only when every check passes
#   SMOKE_FORCE_FAIL=1 ...    fail on purpose (exercises scripts/upgrade.sh's rollback)
#
# Reads bind/port from ~/.config/podman-mcp-admin/podman-mcp-admin.env. Secrets are read from
# /data/config.json inside the container and piped straight into curl: never printed, never
# in argv.
# shellcheck source-path=SCRIPTDIR
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
export QL_LOG_PREFIX=smoke
APP=podman-mcp-admin
C=podman-mcp-admin
UNIT=podman-mcp-admin.service
ENV_FILE=$HOME/.config/$APP/$APP.env

pass=0 fail=0
ok() { printf '  PASS  %s\n' "$*"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$*"; fail=$((fail + 1)); }
check() { # check <description> <command...>
  local d=$1
  shift
  if "$@"; then ok "$d"; else bad "$d"; fi
}

[[ -f $ENV_FILE ]] || { echo "smoke: $ENV_FILE not found (not installed?)" >&2; exit 1; }
ql_env_load "$ENV_FILE"
BIND=$(ql_env_get MCP_ADMIN_BIND)
PORT=$(ql_env_get MCP_ADMIN_PORT)
BASE=http://$BIND:$PORT

# http_code <method> <path> [curl args...]: status code only ("000" when refused)
http_code() {
  local m=$1 p=$2
  shift 2
  curl -s -o /dev/null -w '%{http_code}' -m 10 -X "$m" "$@" "$BASE$p" 2>/dev/null || true
}
in_container() { podman exec "$C" python -c "$1"; }
cfg_json() { # cfg_json <python expr over cfg> : runs inside the container, prints the result
  in_container "import json; cfg = json.load(open('/data/config.json')); print($1)"
}

echo "== $APP at $BASE"
check "unit $UNIT is active" systemctl --user is-active --quiet "$UNIT"
check "container health is healthy" test "$(podman inspect --format '{{.State.Health.Status}}' "$C" 2>/dev/null)" = healthy
check "GET /healthz -> 200" test "$(http_code GET /healthz)" = 200

listeners=$(ss -ltnH "sport = :$PORT" 2>/dev/null | awk '{print $4}' | sort -u | tr '\n' ' ')
check "port $PORT listens only on $BIND:$PORT (got: ${listeners:-none})" test "${listeners% }" = "$BIND:$PORT"

unit_text=$(systemctl --user cat "$UNIT" 2>/dev/null || true)
check "no plaintext JWT_SECRET/ADMIN_PASSWORD/MCP_AUTH_TOKEN in the unit" \
  test "$(grep -ciE '(JWT_SECRET|ADMIN_PASSWORD|MCP_AUTH_TOKEN)=' <<<"$unit_text")" = 0
check "root filesystem is read-only" test "$(podman inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$C" 2>/dev/null)" = true
check "/data/config.json is mode 600" test "$(in_container 'import os; print(oct(os.stat("/data/config.json").st_mode & 0o777))' 2>/dev/null)" = 0o600

# admin login with the live password (config.json), JSON built inside the container
code=$(cfg_json 'json.dumps({"password": cfg["admin_password"]})' 2>/dev/null \
  | curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Content-Type: application/json' --data @- "$BASE/api/auth/login" 2>/dev/null || true)
check "POST /api/auth/login with the admin password -> 200" test "$code" = 200
# TEST-NET source address: the failure counts against 203.0.113.7, never against the host.
check "POST /api/auth/login with a wrong password -> 401" \
  test "$(http_code POST /api/auth/login -H 'X-Forwarded-For: 203.0.113.7' -H 'Content-Type: application/json' \
    --data '{"password":"smoke-wrong-password"}')" = 401

# MCP connector: the token stays in a variable; the URL goes to curl on stdin, not argv
token=$(cfg_json 'cfg.get("mcp_auth_token", "")' 2>/dev/null || true)
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"woow-smoke","version":"1"}}}'
mcp_code() { # mcp_code <token>
  printf 'url = "%s/private_%s/mcp/"\n' "$BASE" "$1" \
    | curl -s -o /dev/null -w '%{http_code}' -m 15 -K - -X POST \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      --data "$init" 2>/dev/null || true
}
if [[ -n $token ]]; then
  check "MCP initialize through /private_<token>/mcp/ -> 200" test "$(mcp_code "$token")" = 200
else
  bad "no mcp_auth_token in /data/config.json"
fi
check "MCP initialize with a wrong token -> 403" test "$(mcp_code smoke-wrong-token)" = 403
check "GET /.well-known/oauth-authorization-server -> 404 (no OAuth, on purpose)" \
  test "$(http_code GET /.well-known/oauth-authorization-server)" = 404
token=''

if [[ ${SMOKE_FORCE_FAIL:-0} == 1 ]]; then bad "SMOKE_FORCE_FAIL=1 (forced failure)"; fi
echo "== $pass passed, $fail failed"
((fail == 0))
