"""Preserve n8n test-run backend messages after the pinned native direct-input patch.

Port of HA's reviewed handleTestRunError block. Fixed hints retain their existing
text and append n8n's own message once, including 404/405 route/version hints.
The exact input is direct-patched n8n-mcp 2.96.2; no server/isError patch is changed.
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
ORIGINAL_SHA256 = "4af5ded41cefea7999871df4208746f3c6b8d3a8d4551019b724e77eec2f5ff4"
PATCHED_SHA256 = "fe3181292ec5d1ad07195f55a7ab73569b15d990f687f620ecaf633742fb24aa"
HANDLER = Path("dist/mcp/handlers-n8n-manager.js")
REPLACEMENTS = (
    (b"""    if (error instanceof n8n_errors_1.N8nApiError) {
        if (error.statusCode === 402) {
            return { success: false, error: TEST_RUN_QUOTA_HINT, code: error.code };
        }
        if (error.statusCode === 403) {
            return { success: false, error: options.scopeHint, code: error.code };
        }
        if (error.statusCode === 409 && options.conflictHint) {
            return { success: false, error: options.conflictHint, code: error.code };
        }
        if (error.statusCode === 404 || error.statusCode === 405) {
            const gate = await testRunRouteGate(error.statusCode, context, options);
            if (gate) {
                return { success: false, error: gate, code: error.code };
            }
        }
        if (error.statusCode === 404) {
            return { success: false, error: options.notFoundHint, code: error.code };
        }
""",
     b"""    if (error instanceof n8n_errors_1.N8nApiError) {
        // PATCH(woowtech 0.2.2): every fixed hint also carries n8n's own message (owner decision 2026-10-15: backend
        // errors reach the client with the backend's text), e.g. "Evaluations are not available on your plan".
        const own = typeof error.message === 'string' ? error.message.trim() : '';
        const withN8n = (hint) => own && !String(hint).includes(own) ? `${hint} n8n: ${own}` : hint;
        if (error.statusCode === 402) {
            return { success: false, error: withN8n(TEST_RUN_QUOTA_HINT), code: error.code };
        }
        if (error.statusCode === 403) {
            return { success: false, error: withN8n(options.scopeHint), code: error.code };
        }
        if (error.statusCode === 409 && options.conflictHint) {
            return { success: false, error: withN8n(options.conflictHint), code: error.code };
        }
        if (error.statusCode === 404 || error.statusCode === 405) {
            const gate = await testRunRouteGate(error.statusCode, context, options);
            if (gate) {
                return { success: false, error: withN8n(gate), code: error.code };
            }
        }
        if (error.statusCode === 404) {
            return { success: false, error: withN8n(options.notFoundHint), code: error.code };
        }
"""),
)


def patch_package(package: Path) -> bool:
    """Atomically patch the exact pinned artifact; return False if already patched."""
    if json.loads((package / "package.json").read_text())["version"] != VERSION:
        raise ValueError("Unreviewed n8n-mcp version; update the backend-message patch")
    handler = package / HANDLER
    if handler.is_symlink():
        raise ValueError("Expected a regular upstream handler")
    original = handler.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest == PATCHED_SHA256:
        return False
    if digest != ORIGINAL_SHA256:
        raise ValueError("Unreviewed n8n-mcp handler bytes; refusing to modify them")
    patched = original
    for before, after in REPLACEMENTS:
        if patched.count(before) != 1:
            raise ValueError("Expected exactly one occurrence of each test-run error block")
        patched = patched.replace(before, after, 1)
    if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
        raise ValueError("Patched n8n-mcp handler digest mismatch")
    mode = stat.S_IMODE(handler.stat().st_mode)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=handler.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(patched)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, handler)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return True


def main(argv: list[str] | None = None) -> int:
    """Apply the guarded patch during Docker build; errors fail the build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args(argv)
    changed = patch_package(args.package)
    print(f"n8n-mcp {VERSION}: backend-message patch {'applied' if changed else 'verified'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
