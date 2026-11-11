# Headscale MCP Podman Implementation Plan

**Goal:** Add a Headscale MCP product to this repository with the same 29 native operations, GUI, access controls, and persisted state behavior as the current PaaS source, packaged for rootless Podman with Quadlet.

**Architecture:** Keep the existing Podman-host MCP installation intact. Establish `products/` as the workspace for subsequent MCP products, with a single shared `mcp-admin-core` source and product-owned app code. Vendor the exact PaaS Headscale app and core at a recorded commit, patch only the deployment capability kind, and run the same app in a product-specific Quadlet. Use the existing Quadlet library for host lifecycle operations.

**Tech Stack:** Python 3.12, uv, FastMCP 4.1.3, MCP 2.3.0, FastAPI, pytest, rootless Podman 4.9+, systemd user Quadlet.

---

### Task 1: Record parity baseline and import product source

**Files:**
- Create: `products/pyproject.toml`, `products/uv.lock`, `products/PROVENANCE.md`
- Create: `products/packages/mcp-admin-core/**`, `products/apps/headscale/**`
- Create: `products/scripts/check-headscale-parity.py`

**Steps:**
1. Export `apps/headscale` and `packages/mcp-admin-core` from `WOOWTECH/woow-mcp-server` `main` at the recorded SHA. Keep the PaaS app's native 29-operation registry, dedicated GUI and security modules intact.
2. Set `deployment_kind="podman"` in the copied Headscale app. Document this one deployment-specific difference and source hashes in `products/PROVENANCE.md`.
3. Create a `products/` uv workspace with only the Headscale app and shared core. Preserve PaaS resolved versions for protocol/security dependencies and lock them.
4. Make the parity script compare vendored files to a supplied PaaS checkout or archive, allowing only the declared deployment-kind change.
5. Run the source Headscale tests and parity check; commit the imported baseline.

### Task 2: Add first-boot state and product image

**Files:**
- Create: `products/deploy/headscale/bootstrap.py`, `products/deploy/headscale/Dockerfile`
- Test: `products/deploy/headscale/tests/test_bootstrap.py`

**Steps:**
1. Write a failing bootstrap test for empty state, existing state, mode 0600, and refusing weak/missing admin credentials. Confirm the failure.
2. Seed only the first-boot admin password from the Podman secret before importing the PaaS app. Do not overwrite existing state or log credentials; remove the seed env from child process inheritance. Let the shared core issue the initial MCP token as in PaaS.
3. Build the Headscale image from the pinned workspace, install the dedicated static GUI and run as uid/gid 1000 with `/data` persistent.
4. Run bootstrap tests and image import checks; commit.

### Task 3: Add independent Quadlet installation

**Files:**
- Create: `products/deploy/headscale/config/headscale-mcp-admin.env.example`
- Create: `products/deploy/headscale/quadlet/*.container`, `*.volume`, `*.network`, `render-vars`
- Create: `products/deploy/headscale/scripts/install.sh`, `smoke.sh`, `show-connector.sh`
- Test: `products/deploy/headscale/tests/test_install_contract.py`

**Steps:**
1. Write a failing installer contract test covering separate names/ports/volume, secret injection, exact Origin and CIDR validation, and no Podman socket mount. Confirm the failure.
2. Adapt the existing `scripts/lib/quadlet-lib.sh` installer path for the Headscale product. Keep the existing Podman-host installer and units unchanged.
3. Use private loopback defaults for local development; require HTTPS cookie policy when a nonlocal Origin is configured. Document the production proxy/Origin/CIDR setup.
4. Validate rendered Quadlet units and dry-run with the existing shims/generator; commit.

### Task 4: Verify parity and publish reviewable change

**Files:**
- Modify: `README.md`, `README_zh-TW.md`
- Create: `.github/workflows/headscale-product.yml`

**Steps:**
1. Run all 29 native-operation tests, Headscale GUI/security tests, bootstrap/installer tests, and existing Podman-host regression tests in isolated environments.
2. Build the image in CI (local Podman is unavailable in this workspace); run container startup, login, token, wrong-Origin and MCP initialization smoke checks in an ephemeral host when a runner supports Podman.
3. Document exact source commit, dependency versions, supported origins, private destination allowance, backup/restore and upgrade behavior.
4. Review `git diff`, run `git diff --check`, commit, push the branch and open a PR. Merge only after all required checks and review gates are met.

**Release constraints:** Existing Podman-host runtime uses MCP SDK 1.x; the Headscale app uses MCP SDK 2.x. Keep separate Python workspaces/images until the Podman-host app is upgraded. The intended parity is Headscale product behavior, not sharing a single process or dependency lock with the old Podman-host product.
