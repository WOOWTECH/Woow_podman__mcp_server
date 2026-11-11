"""Build-time native execute_workflow compatibility for pinned n8n-mcp 2.96.2.

Start untagged; retry once only for the exact SDK pre-dispatch inputs.type
validation rejection. Cache successful formats by official client identity, and
stop negotiation if that client changes. Unrelated validation, authorization and
execution errors never trigger a retry. Once a tagged retry is sent, return its
actual result even if it fails: execution may have started before a timeout.
Callers must reconcile ambiguous outcomes before explicitly retrying. Version,
input bytes and combined patch digests are checked before atomic replacement.
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
ORIGINAL_SHA256 = "6860193f1217c222c1ee1b1b08b7beb38847b85a182c36f2d8b8be7339cfad7b"
PATCHED_SHA256 = "4af5ded41cefea7999871df4208746f3c6b8d3a8d4551019b724e77eec2f5ff4"
HANDLER = Path("dist/mcp/handlers-n8n-manager.js")
REPLACEMENTS = (
    (b"""const OFFICIAL_TEST_ACTION = 'test_workflow';""",
     b"""// PATCH(woowtech): native input formats belong to an official client, never a tenant-global flag.
const woowDirectInputFormats = new WeakMap();
const OFFICIAL_TEST_ACTION = 'test_workflow';"""),
    (b"""            const response = await routeOfficial(['execute_workflow'], {
                workflowId: input.workflowId,
                executionMode: input.executionMode ?? 'manual',
                ...(directTriggerNode ? { triggerNodeName: directTriggerNode } : {}),
                ...(inputs ? { inputs } : {}),
            }, false, 'direct');""",
     b"""            const nativeClient = (0, official_mcp_access_1.getOfficialMcpClient)(context);
            const knownFormat = nativeClient ? woowDirectInputFormats.get(nativeClient) : undefined;
            const sameClient = () => nativeClient && nativeClient === (0, official_mcp_access_1.getOfficialMcpClient)(context);
            const execute = (tagged) => routeOfficial(['execute_workflow'], {
                workflowId: input.workflowId,
                executionMode: input.executionMode ?? 'manual',
                ...(directTriggerNode ? { triggerNodeName: directTriggerNode } : {}),
                ...(inputs ? { inputs: tagged ? {
                    type: 'chatInput' in inputs ? 'chat' : 'formData' in inputs ? 'form' : 'webhook',
                    ...inputs,
                } : inputs } : {}),
            }, false, 'direct');
            const missingInputTag = (result) => {
                const prefix = 'Input validation error: Invalid arguments for tool execute_workflow: ';
                if (result.success !== false || result.code !== 'INVALID_ARGS' ||
                    typeof result.error !== 'string' || !result.error.startsWith(prefix)) return false;
                try {
                    const issues = JSON.parse(result.error.slice(prefix.length));
                    if (!Array.isArray(issues) || issues.length !== 1) return false;
                    const issue = issues[0];
                    const discriminatorIssue = issue?.code === 'invalid_union_discriminator' ||
                        (issue?.code === 'invalid_union' && issue.discriminator === 'type' &&
                         issue.note === 'No matching discriminator' &&
                         Array.isArray(issue.errors) && issue.errors.length === 0);
                    return discriminatorIssue &&
                        JSON.stringify(issue.path) === '["inputs","type"]' &&
                        Array.isArray(issue.options) &&
                        JSON.stringify([...issue.options].sort()) === '["chat","form","webhook"]';
                } catch { return false; }
            };
            let response = await execute(knownFormat === true);
            if (inputs && knownFormat === undefined && sameClient()) {
                if (response.success === true) {
                    woowDirectInputFormats.set(nativeClient, false);
                } else if (missingInputTag(response)) {
                    // Validation rejected the first call before execution; no generic write retry.
                    const retry = await execute(true);
                    if (retry.success === true) {
                        if (sameClient()) woowDirectInputFormats.set(nativeClient, true);
                    }
                    // The retry may have executed before failing: preserve its actual outcome.
                    // Never disguise an uncertain execution as the first pre-dispatch rejection.
                    response = retry;
                }
            }"""),
)


def patch_package(package: Path) -> bool:
    """Atomically patch the exact pinned artifact; return False if already patched."""
    if json.loads((package / "package.json").read_text())["version"] != VERSION:
        raise ValueError("Unreviewed n8n-mcp version; update the compatibility patch")
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
            raise ValueError("Expected exactly one occurrence of each input branch")
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
    print(f"n8n-mcp {VERSION}: direct inputs patch {'applied' if changed else 'verified'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
