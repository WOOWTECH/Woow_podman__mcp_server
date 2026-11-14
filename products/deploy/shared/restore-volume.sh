#!/usr/bin/env bash
# Sourced by the restore scripts; needs only rootless Podman (no systemd), so CI runs it as is.
# Preparation never touches the installed volume: callers stop the service and replace the
# volume only after mcp_prepare_revoked succeeded, so a refused backup leaves the service as it was.

# mcp_prepare_revoked TARFILE IMAGE PASSWORD_SECRET PRODUCT PREPARED
#   Imports TARFILE into a scratch volume and runs restore-revoked.py in IMAGE (the installed
#   product image) to write the core's fully revoked copy into the new volume PREPARED. The
#   scratch volume is always removed; PREPARED is removed too when anything fails.
mcp_prepare_revoked() {
  local tarfile=$1 image=$2 secret=$3 product=$4 prepared=$5 stage rc=0 helper
  helper=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/restore-revoked.py
  stage=${prepared}_source
  podman volume rm -f "$stage" "$prepared" >/dev/null 2>&1 || true
  podman volume create "$stage" >/dev/null && podman volume create "$prepared" >/dev/null || rc=1
  if ((rc == 0)) && ! podman volume import "$stage" "$tarfile"; then
    rc=1
  elif ((rc == 0)) && ! podman run --rm --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges --user 1000:1000 \
      -v "$stage:/restore/source:U" -v "$prepared:/restore/target:U" \
      -v "$helper:/restore/restore-revoked.py:ro" \
      --secret "$secret,type=env,target=ADMIN_PASSWORD" \
      `# The core warns at import without JWT_SECRET; the helper signs nothing.` \
      -e JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
      --entrypoint python "$image" /restore/restore-revoked.py "$product" \
      /restore/source /restore/target; then
    rc=1
  fi
  podman volume rm -f "$stage" >/dev/null 2>&1 || true
  ((rc == 0)) || podman volume rm -f "$prepared" >/dev/null 2>&1 || true
  return "$rc"
}

# mcp_publish_prepared PREPARED VOLUME
#   Copies PREPARED into VOLUME (existing and empty, the service stopped) and removes PREPARED.
mcp_publish_prepared() {
  local prepared=$1 volume=$2
  podman volume export "$prepared" | podman volume import "$volume" - || return 1
  podman volume rm -f "$prepared" >/dev/null 2>&1 || true
}
