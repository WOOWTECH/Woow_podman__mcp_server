#!/usr/bin/env python3
"""Compare the vendored Headscale app/core to the pinned PaaS source commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "deploy/catalog.json").read_text())["paas_source_commit"]
SOURCE_PATHS = ("apps/headscale", "packages/mcp-admin-core/mcp_admin_core",
                "packages/mcp-admin-core/pyproject.toml")
MAIN = "apps/headscale/headscale_mcp_admin/main.py"


def git(source: Path, *args: str) -> bytes:
    return subprocess.check_output(("git", "-C", str(source), *args))


def expected(path: str, value: bytes) -> bytes:
    if path == MAIN:
        before = b'deployment_kind="paas"'
        assert value.count(before) == 1, "PaaS deployment marker changed"
        return value.replace(before, b'deployment_kind="podman"')
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-checkout", required=True, type=Path)
    args = parser.parse_args()
    tracked = git(args.source_checkout, "ls-tree", "-r", "--name-only", PIN,
                  *SOURCE_PATHS).decode().splitlines()
    failures = []
    for path in tracked:
        local = ROOT / path
        if not local.is_file() or local.read_bytes() != expected(
                path, git(args.source_checkout, "show", f"{PIN}:{path}")):
            failures.append(path)
    extras = [str(path.relative_to(ROOT)) for base in SOURCE_PATHS[:2]
              for path in (ROOT / base).rglob("*") if path.is_file()
              and not any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)
              and str(path.relative_to(ROOT)) not in tracked]
    if failures or extras:
        print(f"parity failed: changed/missing={failures}, extra={extras}", file=sys.stderr)
        return 1
    print(f"parity ok: {len(tracked)} PaaS files at {PIN}; deployment kind=podman")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
