#!/usr/bin/env bash
# Build product images from this clean checkout, labelled the way release_preflight.py checks.
# Usage: products/deploy/build-release-images.sh [PRODUCT...]   (default: every catalog product)
#
# HEAD must already be on origin/main (run `git fetch origin` first), as the unsigned install
# requires. The build context is `git archive HEAD`, so ignored or untracked files never reach
# an image. Tags are the ones the installer looks up (catalog image_version, Headscale VERSION).
# Install or upgrade the result with a signed manifest, or with --skip-release-signature.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
DEPLOY=$REPO/products/deploy
die() { printf 'build-release-images: %s\n' "$*" >&2; exit 1; }

[[ -z $(git -C "$REPO" status --porcelain --untracked-files=all) ]] \
  || die "the checkout has local changes; commit or remove them first"
revision=$(git -C "$REPO" rev-parse HEAD)
git -C "$REPO" merge-base --is-ancestor "$revision" refs/remotes/origin/main \
  || die "HEAD is not on origin/main; fetch and check out a merged commit"
mapfile -t known < <(python3 "$DEPLOY/shared/catalog.py" list | cut -f1)
read -r canonical version < <(python3 -c \
  'import json, sys; c = json.load(open(sys.argv[1])); print(c["paas_source_commit"], c["image_version"])' \
  "$DEPLOY/catalog.json")
if (($#)); then products=("$@"); else products=("${known[@]}"); fi
for product in "${products[@]}"; do
  printf '%s\n' "${known[@]}" | grep -qx -- "$product" || die "unknown catalog product: $product"
done
build_id="local-${revision:0:12}-$(date -u +%Y%m%dT%H%M%SZ)"

context=$(mktemp -d "${TMPDIR:-/tmp}/woow-release-build.XXXXXX")
trap 'rm -rf -- "$context"' EXIT
git -C "$REPO" archive --format=tar HEAD products | tar -x -C "$context"

for product in "${products[@]}"; do
  if [[ $product == headscale ]]; then
    tag="localhost/woow-headscale-mcp-admin:$(<"$DEPLOY/headscale/VERSION")"
    podman build --format docker --build-arg "VCS_REF=$revision" \
      --build-arg "CANONICAL_SOURCE=$canonical" --build-arg "RELEASE_BUILD_ID=$build_id" \
      -t "$tag" -f "$context/products/deploy/headscale/Dockerfile" "$context/products"
  else
    tag="localhost/woow-$product-mcp-admin:$version"
    podman build --format docker --label "org.opencontainers.image.revision=$revision" \
      --label "io.woowtech.canonical-source=$canonical" --label "io.woowtech.release-build=$build_id" \
      -t "$tag" -f "$context/products/apps/$product/Dockerfile" "$context/products"
  fi
  printf 'built %s %s %s\n' "$product" "$tag" "$(podman image inspect --format '{{.Id}}' "$tag")"
done
