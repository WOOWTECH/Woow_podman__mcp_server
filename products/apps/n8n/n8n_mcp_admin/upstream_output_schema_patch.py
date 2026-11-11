"""Guarded n8n-mcp 2.96.2 output-schema correction for validate_workflow.

The pinned validator emits object-valued ``details`` for node-group warnings,
AI-node issues and misplaced node-level properties. Upstream tools.js declares
both error/warning ``details`` as strings, so MCP SDK clients reject valid
structured results. Only those two schema type literals are changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

VERSION = "2.96.2"
REL = Path("dist/mcp/tools.js")
ORIGINAL_SHA256 = "acb4d2a3ec74b5f2b41d327a93340a0f9a799e99947656afd010a76390fb10cb"
PATCHED_SHA256 = "e30bddb3967661d36ae5dd8c6b5eb8b7e1b56f6053b2bb972543d7bbc4d57803"
BEFORE = b"details: { type: 'string' }"
AFTER = b"details: { type: 'object' }"


def patch_package(package: Path) -> bool:
    if json.loads((package / "package.json").read_text())["version"] != VERSION:
        raise ValueError("Unreviewed n8n-mcp version")
    target = package / REL
    if target.is_symlink() or not target.is_file():
        raise ValueError("Expected regular upstream tools.js")
    original = target.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest == PATCHED_SHA256:
        return False
    if digest != ORIGINAL_SHA256 or original.count(BEFORE) != 2:
        raise ValueError("Unreviewed n8n-mcp tools.js bytes")
    updated = original.replace(BEFORE, AFTER)
    if hashlib.sha256(updated).hexdigest() != PATCHED_SHA256:
        raise ValueError("Patched tools.js digest mismatch")
    mode = stat.S_IMODE(target.stat().st_mode)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    changed = patch_package(args.package)
    print(f"n8n-mcp {VERSION}: workflow output schema {'patched' if changed else 'verified'}")


if __name__ == "__main__":
    main()
