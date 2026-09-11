#!/usr/bin/env bash
# scripts/show-connector.sh: print the MCP connector URL of this deployment.
#
#   scripts/show-connector.sh [--base URL]
#
#   --base URL   public base URL to print instead of the local one, e.g. the tunnel
#                hostname: --base https://podman-mcp.example.com
#
# The token comes from /data/config.json inside the running container, which is where a
# rotation from the Tokens page lands. When the container is down it falls back to the
# podman secret podman-mcp-admin-mcp-token, which only seeded config.json on first boot.
# The URL is a bearer credential: anyone holding it controls this user's podman at the
# configured profile. Do not paste it into tickets or chat.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
APP=podman-mcp-admin
ENV_FILE=$HOME/.config/$APP/$APP.env

base=''
while (($#)); do
  case $1 in
    --base) base=${2:?--base needs a URL}; shift ;;
    -h | --help) sed -n '2,13p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
if [[ -z $base ]]; then
  [[ -f $ENV_FILE ]] || ql_die "$ENV_FILE not found; run scripts/install.sh first"
  ql_env_load "$ENV_FILE"
  base="http://$(ql_env_get MCP_ADMIN_BIND):$(ql_env_get MCP_ADMIN_PORT)"
fi
base=${base%/}

token=''
if [[ $(podman inspect --format '{{.State.Status}}' podman-mcp-admin 2>/dev/null || true) == running ]]; then
  token=$(podman exec podman-mcp-admin python -c \
    'import json; print(json.load(open("/data/config.json")).get("mcp_auth_token", ""))') || token=''
fi
if [[ -z $token ]]; then
  ql_warn "container not running (or config.json unreadable): using the first-boot seed secret"
  token=$(podman secret inspect --showsecret --format '{{.SecretData}}' podman-mcp-admin-mcp-token) \
    || ql_die "no running container and no podman secret podman-mcp-admin-mcp-token"
fi
printf '%s/private_%s/mcp/\n' "$base" "$token"
