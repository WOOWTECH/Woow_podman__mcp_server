#!/usr/bin/env bash
# Print the live Headscale MCP connector URL; it is a bearer credential.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
# shellcheck source=../../../../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=headscale-mcp-admin
ENV_FILE=$HOME/.config/$APP/$APP.env
base=''
while (($#)); do
  case $1 in
    --base) base=${2:?--base needs a URL}; shift ;;
    -h | --help) printf 'Usage: %s [--base https://mcp.example.com]\n' "$0"; exit 0 ;;
    *) ql_die "unknown option $1" ;;
  esac
  shift
done
if [[ -z $base ]]; then
  [[ -f $ENV_FILE ]] || ql_die "$ENV_FILE not found"
  ql_env_load "$ENV_FILE"
  base="http://$(ql_env_get MCP_ADMIN_BIND):$(ql_env_get MCP_ADMIN_PORT)"
fi
base=${base%/}
[[ $base == https://* || $base == http://127.0.0.1:* ]] \
  || ql_die "--base must be HTTPS or the local HTTP URL"
[[ $(podman inspect --format '{{.State.Status}}' "$APP" 2>/dev/null || true) == running ]] \
  || ql_die "$APP is not running; the current token is only in its persisted state"
token=$(podman exec "$APP" python -c \
  'import json; print(json.load(open("/data/config.json")).get("mcp_auth_token", ""))')
[[ -n $token ]] || ql_die "no active MCP token; rotate it in the Tokens page"
printf '%s/private_%s/mcp\n' "$base" "$token"
