# shellcheck shell=bash
# tests/dryrun.local.sh: repo-specific checks, sourced at the end of tests/dryrun.sh (which
# defines REPO, WORK, APP, failures and run_variant). CI runs it through tests/dryrun.sh.
# shellcheck disable=SC2154 # REPO and failures come from tests/dryrun.sh

local_fail() { echo "FAIL $*"; failures=$((failures + 1)); }

# One version, three places: VERSION, the unit's Image= tag, pyproject.toml.
ver=$(<"$REPO/VERSION")
img_tag=$(sed -n 's|^Image=localhost/woow-podman-mcp-admin:||p' "$REPO/quadlet/podman-mcp-admin.container")
py_ver=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$REPO/pyproject.toml" | head -n1)
if [[ $ver == "$img_tag" && $ver == "$py_ver" ]]; then
  echo "ok   versions agree ($ver)"
else
  local_fail "versions disagree: VERSION=$ver Image= tag=$img_tag pyproject=$py_ver"
fi

# Loopback by default: the example must never publish the podman socket beyond this host.
if grep -qx 'MCP_ADMIN_BIND=127.0.0.1' "$REPO/config/podman-mcp-admin.env.example"; then
  echo "ok   example binds 127.0.0.1"
else
  local_fail "config/podman-mcp-admin.env.example must default to MCP_ADMIN_BIND=127.0.0.1"
fi

# Base images pinned to exact versions (STANDARD section 3).
if grep -E '^FROM ' "$REPO/Dockerfile" | grep -qvE ':[0-9]+\.[0-9]+\.[0-9]+-'; then
  local_fail "Dockerfile has a FROM line without an exact version: $(grep -E '^FROM ' "$REPO/Dockerfile" | tr '\n' ' ')"
else
  echo "ok   Dockerfile base images pinned"
fi
