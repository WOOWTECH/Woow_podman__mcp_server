#!/usr/bin/env bash
# Source after quadlet-lib.sh. The first check is side-effect-free; the second
# runs under the existing per-app lock to close an installer/upgrade race.
#
# --skip-release-signature (owner decision, 2026-11-14) sets MCP_RELEASE_UNSIGNED=1 after this
# file is sourced. There is then no manifest or signature: the local image must carry the clean
# checkout's commit and the catalog pin in its labels, and its ID is still rendered into Quadlet.
MCP_RELEASE_UNSIGNED=0

mcp_release_preflight() {
  local product=$1 release_manifest=$2 source_checkout=$3
  if [[ $MCP_RELEASE_UNSIGNED == 1 ]]; then
    python3 "$REPO/products/deploy/release_preflight.py" --repo "$REPO" \
      --product "$product" --unsigned
  else
    python3 "$REPO/products/deploy/release_preflight.py" --repo "$REPO" \
      --product "$product" --release-manifest "$release_manifest" \
      --source-checkout "$source_checkout"
  fi
}

mcp_release_abort_locked() {
  local app=$1 message=$2 state_dir state_root
  state_dir=$(_ql_state_dir "$app")
  state_root=${state_dir%/*}
  ql_unlock "$app"
  [[ ${MCP_RELEASE_NEW_STATE:-0} != 1 ]] || rmdir -- "$state_dir" 2>/dev/null || true
  [[ ${MCP_RELEASE_NEW_ROOT:-0} != 1 ]] || rmdir -- "$state_root" 2>/dev/null || true
  ql_die "$message"
}

mcp_release_gate() {
  local app=$1 product=$2 release_manifest=$3 source_checkout=$4
  local state_dir state_root had_state=0 had_root=0 first_id locked_id
  if [[ $MCP_RELEASE_UNSIGNED == 1 ]]; then
    [[ -z $release_manifest && -z $source_checkout ]] \
      || ql_die "--skip-release-signature cannot be combined with --release-manifest or --source-checkout"
    ql_warn "release signature skipped by the operator: $product is checked only against this clean checkout"
  else
    [[ -n $release_manifest && -n $source_checkout ]] \
      || ql_die "reviewed --release-manifest and --source-checkout are required"
  fi
  first_id=$(mcp_release_preflight "$product" "$release_manifest" "$source_checkout") \
    || ql_die "release provenance preflight failed before persistent state change"

  state_dir=$(_ql_state_dir "$app")
  state_root=${state_dir%/*}
  [[ -d $state_dir ]] && had_state=1
  [[ -d $state_root ]] && had_root=1
  ql_lock "$app"
  MCP_RELEASE_NEW_STATE=$((1 - had_state))
  MCP_RELEASE_NEW_ROOT=$((1 - had_root))
  if ! locked_id=$(mcp_release_preflight "$product" "$release_manifest" "$source_checkout"); then
    mcp_release_abort_locked "$app" "release provenance changed while acquiring the install lock"
  fi
  if [[ $first_id != "$locked_id" ]]; then
    mcp_release_abort_locked "$app" "reviewed image identity changed while acquiring the install lock"
  fi
  RELEASE_IMAGE_ID=$locked_id
}

mcp_release_recheck() {
  local app=$1 product=$2 release_manifest=$3 source_checkout=$4 current_id
  current_id=$(mcp_release_preflight "$product" "$release_manifest" "$source_checkout") \
    || mcp_release_abort_locked "$app" "release provenance changed before persistent install"
  [[ $current_id == "$RELEASE_IMAGE_ID" ]] \
    || mcp_release_abort_locked "$app" "reviewed image identity changed before persistent install"
}

mcp_release_check_active_collision() {
  local app=$1 unit=$2 qdir=$3 current=''
  if [[ -f $qdir/$app.container ]]; then
    current=$(sed -n 's/^Image=//p' "$qdir/$app.container" | tail -n 1)
    if [[ $current != "$RELEASE_IMAGE_ID" ]] \
        && systemctl --user is-active --quiet "$unit"; then
      mcp_release_abort_locked "$app" "active service uses another image; use upgrade for backup and rollback"
    fi
  fi
}
