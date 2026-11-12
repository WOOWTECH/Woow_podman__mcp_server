#!/usr/bin/env bash
# Validate the rendered Headscale Quadlet without creating a container.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
DEPLOY=$REPO/products/deploy/headscale
# shellcheck source=../../../../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
EXAMPLE=$DEPLOY/config/headscale-mcp-admin.env.example
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export QL_ENV_MODE_CHECK=0
ql_env_load "$EXAMPLE"
python3 "$DEPLOY/validate_env.py" \
  --bind "$(ql_env_get MCP_ADMIN_BIND)" \
  --port "$(ql_env_get MCP_ADMIN_PORT)" \
  --origins "$(ql_env_get HEADSCALE_MCP_ALLOWED_ORIGINS)" \
  --secure-cookie "$(ql_env_get HEADSCALE_MCP_SECURE_COOKIE)" \
  --cidrs "$(ql_env_get HEADSCALE_MCP_ALLOWED_PRIVATE_CIDRS)"
mkdir -p "$WORK/out"
ql_render "$DEPLOY/quadlet" "$EXAMPLE" "$DEPLOY/quadlet/render-vars" "$WORK/out" \
  "IMAGE=sha256:$(printf 'a%.0s' {1..64})"
ql_lint_policy "$WORK/out"
ql_dryrun "$WORK/out" --verify
echo "Headscale Quadlet render and generator dry-run passed"
