"""Build-time patch for n8n-mcp 2.96.2: tool failures must set ``isError`` (DATA-NMCP-005-2).

The pinned upstream wraps n8n API failures as ``{success: false, error, code: 'API_ERROR'}`` text but
leaves ``CallToolResult.isError`` false, so clients that rely on the flag (Claude Code, agents) treat a
refused ``n8n_delete_workflow`` on a published workflow as success. Two reviewed edits:

- ``dist/mcp/server.js``: when a tool handler returns an object with ``success === false``, the MCP
  response gets ``isError: true``. Text content is unchanged; ``validate_*`` results (``valid: false``
  is an answer, not a failure) and plain-text results are untouched.
- ``dist/mcp/tools-n8n-manager.js``: the ``n8n_delete_workflow`` description says a published workflow
  must be deactivated first.

Like ``upstream_direct_patch``, unreviewed package versions or bytes are rejected instead of patched,
and every file is verified before any file is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import NamedTuple

VERSION = "2.96.2"


class FilePatch(NamedTuple):
    original_sha256: str
    patched_sha256: str
    replacements: tuple[tuple[bytes, bytes], ...]


_RESULT_TAIL = (
    b"                if (name.startsWith('validate_') && structuredContent !== null) {\n"
    b"                    mcpResponse.structuredContent = structuredContent;\n"
    b"                }\n"
    b"                return mcpResponse;"
)
_RESULT_TAIL_PATCHED = (
    b"                if (name.startsWith('validate_') && structuredContent !== null) {\n"
    b"                    mcpResponse.structuredContent = structuredContent;\n"
    b"                }\n"
    b"                if (result !== null && typeof result === 'object' && result.success === false) {\n"
    b"                    mcpResponse.isError = true;\n"
    b"                }\n"
    b"                return mcpResponse;"
)
_DELETE_DESCRIPTION = b"description: `Permanently delete a workflow. This action cannot be undone.`,"
_DELETE_DESCRIPTION_PATCHED = (
    b"description: `Permanently delete a workflow. This action cannot be undone. "
    b"A published (active) workflow cannot be deleted: deactivate it first with n8n_update_partial_workflow "
    b"(operations: [{type: \"deactivateWorkflow\"}]), then delete it.`,"
)

FILES: dict[Path, FilePatch] = {
    Path("dist/mcp/server.js"): FilePatch(
        original_sha256="a1237f5cafe34cddcecb85c48bc467c38a443be487a34b0f24c5cbbb343b30e9",
        patched_sha256="638b148f91c18d484b8bcede838be1032f82ddd1b3aa53b24cf005325ae3d9f7",
        replacements=((_RESULT_TAIL, _RESULT_TAIL_PATCHED),),
    ),
    Path("dist/mcp/tools-n8n-manager.js"): FilePatch(
        original_sha256="28d54ea6a3001678fd510542c82c04c26b2ff3b55b7db63b630800e82e02191f",
        patched_sha256="e31acfb4d4344704786d4a69fc4a2a0a6b784a265df3474073dd5bc5951efac7",
        replacements=((_DELETE_DESCRIPTION, _DELETE_DESCRIPTION_PATCHED),),
    ),
}


def _patched_bytes(rel: Path, spec: FilePatch, original: bytes) -> bytes:
    patched = original
    for before, after in spec.replacements:
        if patched.count(before) != 1:
            raise ValueError(f"Expected exactly one occurrence of the reviewed snippet in {rel}")
        patched = patched.replace(before, after, 1)
    if hashlib.sha256(patched).hexdigest() != spec.patched_sha256:
        raise ValueError(f"Patched {rel} digest mismatch")
    return patched


def _write_atomic(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def patch_package(package: Path) -> bool:
    """Patch the exact pinned artifact; return False if every file was already patched."""
    if json.loads((package / "package.json").read_text())["version"] != VERSION:
        raise ValueError("Unreviewed n8n-mcp version; update the isError compatibility patch")
    pending: list[tuple[Path, bytes]] = []
    for rel, spec in FILES.items():
        path = package / rel
        if path.is_symlink():
            raise ValueError(f"Expected a regular upstream file: {rel}")
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest == spec.patched_sha256:
            continue
        if digest != spec.original_sha256:
            raise ValueError(f"Unreviewed n8n-mcp bytes in {rel}; refusing to modify them")
        pending.append((path, _patched_bytes(rel, spec, original)))
    for path, data in pending:
        _write_atomic(path, data)
    return bool(pending)


def main(argv: list[str] | None = None) -> int:
    """Apply the guarded patch during Docker build; errors fail the build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args(argv)
    changed = patch_package(args.package)
    print(f"n8n-mcp {VERSION}: isError patch {'applied' if changed else 'verified'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
