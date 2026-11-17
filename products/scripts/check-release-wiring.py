#!/usr/bin/env python3
"""Check the release image bindings that installs and upgrades rely on.

Since 2026-11-17 this repository owns the product source (decision 10 in
docs/plans/2026-11-14-multi-profile-alignment.md), so pull requests here review
it and there is no vendored-source snapshot to refresh. This check keeps the
bindings that snapshot also guarded: the Headscale Quadlet runs only the image
the release gate resolved, the Headscale installer is wired to that gate, and
the shared image version names the PaaS fork base recorded in the catalog.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def check() -> dict[str, str]:
    catalog = json.loads((ROOT / "deploy/catalog.json").read_text())
    fork_base = catalog["paas_source_commit"]
    version = (ROOT / "deploy/headscale/VERSION").read_text().strip()
    quadlet = (ROOT / "deploy/headscale/quadlet/headscale-mcp-admin.container").read_text()
    image_lines = [line for line in quadlet.splitlines()
                   if re.match(r"^\s*Image\s*=", line)]
    if image_lines != ["Image=@@IMAGE@@"]:
        raise ValueError("Headscale Quadlet must have exactly one signed image placeholder")
    install = (ROOT / "deploy/headscale/scripts/install.sh").read_text()
    render_vars = (ROOT / "deploy/headscale/quadlet/render-vars").read_text().splitlines()
    if ("mcp_release_gate \"$APP\" headscale" not in install
            or "IMAGE=$IMAGE" not in install
            or "IMAGE" not in render_vars):
        raise ValueError("Headscale signed image renderer is not wired to release gate")
    if not catalog["image_version"].startswith(f"paas-{fork_base[:7]}-podman."):
        raise ValueError("shared image version and PaaS fork base differ")
    return {
        "fork_base": fork_base,
        "headscale_version": version,
        "shared_image_version": catalog["image_version"],
    }


def main() -> int:
    try:
        state = check()
    except (OSError, KeyError, ValueError) as exc:
        print(f"release wiring check failed: {exc}", file=sys.stderr)
        return 1
    print(f"release wiring ok: Headscale {state['headscale_version']}, shared images "
          f"{state['shared_image_version']}, PaaS fork base {state['fork_base']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
