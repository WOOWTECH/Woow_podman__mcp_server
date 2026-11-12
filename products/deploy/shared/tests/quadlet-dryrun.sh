#!/usr/bin/env bash
# Render every common product with the local Podman Quadlet generator.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)
DEPLOY=$REPO/products/deploy
# shellcheck source=../../../../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
envfile=$work/product.env
printf 'MCP_ADMIN_BIND=127.0.0.1\nMCP_ADMIN_PORT=18500\n' >"$envfile"
chmod 600 "$envfile"
ql_env_load "$envfile"
for product in $(python3 "$DEPLOY/shared/catalog.py" list | awk '$1 != "headscale" {print $1}'); do
  app=$product-mcp-admin
  # The installer renders the signed manifest's immutable image ID, not its tag.
  image=sha256:$(printf 'a%.0s' {1..64})
  volume=${app//-/_}_data
  mkdir -p "$work/src" "$work/out"
  for ext in container volume network; do
    cp -- "$DEPLOY/shared/quadlet/product.$ext" "$work/src/$app.$ext"
  done
  ql_render "$work/src" "$envfile" "$DEPLOY/shared/quadlet/render-vars" "$work/out" \
    "PRODUCT=$product" "APP=$app" "IMAGE=$image" "VOLUME=$volume" "ENV_FILE=$envfile"
  ql_lint_policy "$work/out"
  ql_dryrun "$work/out" --verify
  rm -f -- "$work/src"/* "$work/out"/*
done
printf 'Nine common product Quadlet units rendered and verified\n'
