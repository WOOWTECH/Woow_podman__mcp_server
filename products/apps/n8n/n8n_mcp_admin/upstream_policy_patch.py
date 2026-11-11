"""2.96.2 defense in depth: credentials has an action dispatcher but no map entry.

Run after upstream_result_patch. The public proxy remains authoritative for
fresh saved policy; this single native entry also enforces applied env denies.
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
TOOLS = Path("dist/mcp/tools-n8n-manager.js")
ORIGINAL_SHA256 = "e31acfb4d4344704786d4a69fc4a2a0a6b784a265df3474073dd5bc5951efac7"
PATCHED_SHA256 = "cf9107e1abff71e99ff2edcdeee7864c4b7e401ffb6fb124807d04d5fc75cb1d"
BEFORE = b"    'n8n_manage_folders': 'action',\n"
AFTER = BEFORE + b"    'n8n_manage_credentials': 'action',\n"


def patch_package(package: Path) -> bool:
    if json.loads((package / "package.json").read_text())["version"] != VERSION:
        raise ValueError("Unreviewed n8n-mcp version")
    target = package / TOOLS
    if target.is_symlink():
        raise ValueError("Expected regular upstream file")
    original = target.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest == PATCHED_SHA256:
        return False
    if digest != ORIGINAL_SHA256 or original.count(BEFORE) != 1:
        raise ValueError("Unreviewed n8n-mcp policy bytes; run result patch first")
    patched = original.replace(BEFORE, AFTER, 1)
    if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
        raise ValueError("Patched policy digest mismatch")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(patched)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    changed = patch_package(parser.parse_args().package)
    print(f"n8n-mcp {VERSION}: credentials policy {'applied' if changed else 'verified'}")
