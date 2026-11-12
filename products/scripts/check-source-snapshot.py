#!/usr/bin/env python3
"""Check the reviewed PaaS vendor snapshot without CI access to private Gitea.

Refresh is allowed only after both direct parity checks pass against the exact
canonical commit. GitHub CI then verifies the resulting tracked source bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "source-parity-snapshot.json"
HEADSCALE = Path(__file__).with_name("check-headscale-parity.py")
ALL = Path(__file__).with_name("check-all-parity.py")


def tracked_paths(paths: tuple[str, ...]) -> list[str]:
    output = subprocess.check_output(
        ("git", "-C", str(ROOT), "ls-files", "--", *paths), text=True,
    )
    return sorted(set(output.splitlines()))


def fingerprint(paths: tuple[str, ...]) -> dict[str, str | int]:
    files = tracked_paths(paths)
    digest = hashlib.sha256()
    for name in files:
        path = name.encode()
        content = (ROOT / name).read_bytes()
        digest.update(len(path).to_bytes(4, "big"))
        digest.update(path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return {"files": len(files), "sha256": digest.hexdigest()}


def snapshot() -> dict:
    catalog = json.loads((ROOT / "deploy/catalog.json").read_text())
    pin = catalog["paas_source_commit"]
    version = (ROOT / "deploy/headscale/VERSION").read_text().strip()
    quadlet = (ROOT / "deploy/headscale/quadlet/headscale-mcp-admin.container").read_text()
    image = f"Image=localhost/woow-headscale-mcp-admin:{version}"
    if quadlet.count(image + "\n") != 1:
        raise ValueError("Headscale Quadlet image and VERSION differ")
    if not catalog["image_version"].startswith(f"paas-{pin[:7]}-podman."):
        raise ValueError("shared image version and canonical pin differ")
    headscale_paths = tuple(runpy.run_path(str(HEADSCALE))["SOURCE_PATHS"])
    all_paths = tuple(runpy.run_path(str(ALL))["PATHS"])
    return {
        "schema": "woowtech.podman.source-parity-snapshot.v1",
        "paas_source_commit": pin,
        "headscale_version": version,
        "shared_image_version": catalog["image_version"],
        "headscale": fingerprint(headscale_paths),
        "all_products": fingerprint(all_paths),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-from", type=Path,
                        help="canonical Gitea checkout; refresh only after direct parity passes")
    args = parser.parse_args()
    try:
        if args.refresh_from is not None:
            for script in (HEADSCALE, ALL):
                subprocess.run((sys.executable, str(script), "--source-checkout",
                                str(args.refresh_from)), check=True)
            MANIFEST.write_text(json.dumps(snapshot(), indent=2, sort_keys=True) + "\n")
            print(f"refreshed {MANIFEST.relative_to(ROOT)} from verified canonical source")
            return 0
        expected = json.loads(MANIFEST.read_text())
        actual = snapshot()
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"source snapshot unavailable: {exc}", file=sys.stderr)
        return 1
    if actual != expected:
        for key in actual.keys() | expected.keys():
            if actual.get(key) != expected.get(key):
                print(f"source snapshot mismatch: {key}", file=sys.stderr)
        return 1
    print(f"source snapshot ok: {actual['headscale']['files']} Headscale and "
          f"{actual['all_products']['files']} shared-product files at "
          f"{actual['paas_source_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
