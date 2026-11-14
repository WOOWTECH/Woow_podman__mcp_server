#!/usr/bin/env bash
# Sourced by the restore scripts; needs only rootless Podman (no systemd), so CI runs it as is.
#
# mcp_restore_revoked VOLUME TARFILE IMAGE PASSWORD_SECRET PRODUCT
#   Imports TARFILE into a scratch volume, then runs restore-revoked.py in IMAGE (the installed
#   product image) to write the core's fully revoked copy into VOLUME, which must exist and be
#   empty. The scratch volume is always removed. Returns non-zero on any failure.
mcp_restore_revoked() {
  local volume=$1 tarfile=$2 image=$3 secret=$4 product=$5 stage rc=0
  local helper
  helper=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/restore-revoked.py
  stage=${volume}_restore_$$
  podman volume rm -f "$stage" >/dev/null 2>&1 || true
  podman volume create "$stage" >/dev/null || return 1
  if ! podman volume import "$stage" "$tarfile"; then
    rc=1
  elif ! podman run --rm --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges --user 1000:1000 \
      -v "$stage:/restore/source:U" -v "$volume:/restore/target:U" \
      -v "$helper:/restore/restore-revoked.py:ro" \
      --secret "$secret,type=env,target=ADMIN_PASSWORD" \
      `# The core warns at import without JWT_SECRET; the helper signs nothing.` \
      -e JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
      --entrypoint python "$image" /restore/restore-revoked.py "$product" \
      /restore/source /restore/target; then
    rc=1
  fi
  podman volume rm -f "$stage" >/dev/null 2>&1 || rc=1
  return "$rc"
}
