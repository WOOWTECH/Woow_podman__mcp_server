# Headscale Podman parity and release implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the Headscale Podman source, its pinned Gitea source, release image, and verified Quadlet installation refer to the same tested implementation.

**Architecture:** Gitea `WOOWTECH/woow-mcp-server` is the canonical shared MCP source. Publish the prepared DNS and shared logout fixes there first. Pin the Podman vendor to the resulting exact Gitea merge commit, enforce parity in CI, then build and deploy a uniquely versioned Podman image through the existing Quadlet installer and verify the running image and API.

**Tech Stack:** Python 3.12, uv, pytest, React, npm, Gitea CI, GitHub Actions, rootless Podman, Quadlet.

---

### Task 1: Establish canonical source

**Files:** Gitea PR #132 (`apps/headscale/headscale_mcp_server/http_policy.py`, `apps/headscale/tests/test_native_headscale.py`) and PR #133 (`packages/mcp-admin-core/mcp_admin_core/{app.py,auth/middleware.py,auth/sessions.py,config/store.py}`, `packages/mcp-admin-core/tests/test_auth.py`, `packages/mcp-admin-ui/src/components/Sidebar.jsx`).

1. Review the eight changed paths against Podman `main` and run the focused DNS, auth, and UI suites on their combined candidate.
2. Confirm canonical CI, PR mergeability, and the current `main` SHA; merge the two prepared PRs sequentially if the repository gates allow it.
3. Fetch the new Gitea `main`, verify its source bytes against Podman and record the exact merge SHA.

### Task 2: Pin and continuously enforce parity

**Files:** `products/scripts/check-headscale-parity.py`, `products/deploy/catalog.json`, `products/PROVENANCE.md`, `.github/workflows/headscale-product.yml`, `.github/workflows/all-products.yml`, `products/deploy/headscale/VERSION`.

1. Update both source pins to the exact merged Gitea commit, then run `python3 products/scripts/check-headscale-parity.py --source-checkout PATH` and `python3 products/scripts/check-all-parity.py --source-checkout PATH`; both must report zero differences.
2. Add a CI job that fetches that exact canonical commit and runs both parity scripts, so a later vendor edit fails CI.
3. Advance the Headscale Podman version and image binding together; keep the prior image available for rollback.
4. Run the Headscale product tests, shared auth tests, shell syntax checks, and image/Quadlet smoke when a Podman runtime is available.

### Task 3: Publish and verify the release

**Files:** Podman PR and versioned image provenance receipt; existing `products/deploy/headscale/scripts/{install,upgrade,smoke,restore}.sh`.

1. Publish the reviewed Podman branch through its normal PR and CI gates; verify the merged GitHub `main` SHA and unique release version.
2. Build or obtain an image whose source digest matches that SHA. Verify immutable image identity rather than trusting a mutable tag.
3. On a designated Podman host, back up the existing volume, run the Quadlet upgrade, verify `systemctl --user`, container image digest, GUI/MCP health, logout replay, DNS fallback, and the 29 native Headscale API operations where a live backend is available.
4. If no designated host or backend can be identified, preserve the passing source/release evidence and report the deployment boundary explicitly.
