"""Patch odoo-mcp-server to allow direct create/write/unlink via execute_method.

Problem
-------
The upstream ``execute_method`` tool unconditionally blocks ``create``,
``write``, and ``unlink`` calls, forcing a three-step approval flow
(``preview_write`` -> ``validate_write`` -> ``execute_approved_write``).

Claude.ai frequently drops write-approval tokens across sessions, making
this three-step flow unreliable in practice.

Fix
---
When the environment variable ``ODOO_MCP_ALLOW_DIRECT_WRITES`` is set to
``1`` (or ``true`` / ``yes``), the ``DESTRUCTIVE_METHODS`` guard in
``execute_method`` is bypassed, allowing direct ORM calls.

This patch is **opt-in** and safe to apply unconditionally — the guard is
only lifted when the env var is explicitly set.

Usage
-----
Run once after ``pip install odoo-mcp-server``::

    python patches/fix_direct_writes.py

The script is idempotent — re-running it on an already-patched file is safe.

Tool texts follow the switches (WOOW PaaS, odoo-mcp 1.3.0)
-----------------------------------------------------------
Since the owner decision of 2026-10-25 ("預設全開") PaaS services start the child with
``ODOO_MCP_ALLOW_DIRECT_WRITES=1``, ``ODOO_MCP_ALLOW_UNKNOWN_METHODS=1`` and ``MCP_CHATTER_DIRECT=1``
(``odoo_mcp_admin.seed_defaults.FRESH_ENV``; existing configs are migrated once, then an administrator may switch any
of them off again). odoo-mcp 1.3.0 always tells the model the opposite: the ``method`` parameter of ``execute_method``
says "direct create, write, and unlink are blocked", and ``chatter_post`` says its default mode needs an approval token
from a preview call. A model then takes the three-step approval flow for every write (its tools stay listed, as in the
HA add-on: the approval flow remains available but is no longer required) or expects a preview from ``chatter_post``
that never comes. This patch therefore also
makes both descriptions follow the same switches the code reads: switch on, the text says the call runs directly
(round 3: every combination of the four switches has its own truthful text, see below). The text is computed when the
tool is registered, from the child's environment (a settings change restarts the child). Same approach as the WOOW HA
add-on 0.4.0 (Woow_ha_mcp_addons 97bb174).

One predicate, also in health_check (WOOW PaaS round 2, 2026-10-26)
-----------------------------------------------------------------
``health_check``'s ``runtime`` block listed ``write_execution_enabled`` (ODOO_MCP_ENABLE_WRITES, the approval flow),
``unknown_execute_method_enabled`` and ``chatter_direct_enabled`` but nothing for ODOO_MCP_ALLOW_DIRECT_WRITES, so a
client or auditor could not tell whether execute_method runs create/write/unlink directly. The patch therefore adds
``write_policy.direct_writes_enabled()`` -- the exact expression the guard used to inline -- and makes the guard, the
``method`` text and ``runtime_security_report()`` (``health_check.runtime.direct_writes_enabled``) all call it.

Tightened switches really hold (WOOW PaaS round 3, 2026-10-27)
--------------------------------------------------------------
Round 2's sandbox showed that an administrator's tightening did not hold: with ``ODOO_MCP_ALLOW_UNKNOWN_METHODS=1``,
``ODOO_MCP_ALLOW_DIRECT_WRITES=0`` still let unreviewed methods such as ``action_archive`` write and let
``execute_method`` call ``message_post`` past ``MCP_CHATTER_DIRECT=0``; ``ODOO_MCP_ENABLE_WRITES=0`` (the Settings
page calls it the master write gate) only stopped ``execute_approved_write``. The semantics now live in one module of
pure functions, ``odoo_mcp_admin.write_gates`` (see its docstring for the rules and their precedence), and this patch
only hooks upstream into it:

- ``write_policy.write_switches()`` reads the four switches, each with the predicate that enforces it (upstream
  ``writes_enabled`` / ``chatter_direct_enabled`` / ``truthy_env``, and ``direct_writes_enabled`` added here);
- ``execute_method``'s two upstream guards become one call to ``write_gates.execute_method_refusal`` (the allowlist is
  still read only for side-effect / unknown methods, as upstream does); the call passes the model as well as the method,
  because whether a call posts on the chatter depends on both (``mail.message.create``, ``mail.activity.action_feedback``;
  added after the round 3 review);
- ``chatter_post`` first asks ``write_gates.chatter_post_refusal`` (refused while writes are off);
- the descriptions of ``execute_method``'s ``method`` parameter, ``chatter_post``, ``preview_write``,
  ``validate_write`` and ``execute_approved_write`` and the third ``health_check`` note come from ``write_gates``;
- ``health_check``'s ``broad_unknown_method_mode`` reports the mode in effect (``write_gates.unknown_methods_run``:
  unreviewed methods run directly only when writes, direct writes and unknown methods are all on), not the bare
  ODOO_MCP_ALLOW_UNKNOWN_METHODS switch, which ``unknown_execute_method_enabled`` keeps reporting (added after the
  round 3 review);
- ``validate_write`` answers with an explicit ``valid`` boolean (the verdict of ``success``; every early failure is
  ``valid: false``).

With all four switches on every decision lets the call through and every text is round 2's text, so the all-on
behaviour and tools/list are unchanged (``packages/mcp-admin-core/tests/test_odoo_write_gates.py`` pins the tools/list
digest of round 2, 8c825a5). The patched package imports ``odoo_mcp_admin.write_gates``: without it the child does not
start (fail closed); the Dockerfile imports the patched server once at build time to prove the wiring.

Exact digests: the patch accepts only the published odoo-mcp 1.3.0 ``tools_write.py`` (PINNED) and ``write_policy.py``
(WRITE_POLICY_PINNED), and ``server_core.py`` as left by ``fix_cross_session_token_store.py`` (SERVER_CORE_PINNED; the
Dockerfile runs that patch first), and writes exactly the PATCHED digests. All three files are checked before any of
them is written (all or nothing); already patched files are left as they are and any other content is refused
(exit 1), so a new odoo-mcp release cannot silently ship without these texts. A change of ``odoo_mcp_admin.write_gates``
alone does not change the digests (the hooks stay the same).
"""

from __future__ import annotations

import glob
import hashlib
import os
import sys

MARKER = "PATCH(woowtech): one decision for the four write switches"

PINNED = "d02045c6c268c664f4f58036c7cf1862e7fcb602ec84d9f43630e7bf7b1e749b"  # odoo-mcp 1.3.0 tools_write.py as published
PATCHED = "fb60e74371c56dd87290f81564db0f072c3debfadd8a26f45452b9fc0c54a619"  # tools_write.py after this patch
WRITE_POLICY_PINNED = "d2fa967f7efc942abfb7792608c6323c77fcda81def86fd7c0b265102415b154"  # 1.3.0 write_policy.py
WRITE_POLICY_PATCHED = "9225ce306d1f2d7cc2dd20043a44c2f7c6e4f8be3b4b7ce1d3d62175503e7f2e"
SERVER_CORE_PINNED = "4866870f36f905122f2a6fbdb105591268b51d26848a38b99cf25efb498bee1d"  # 1.3.0 server_core.py after fix_cross_session_token_store
SERVER_CORE_PATCHED = "4d65d4021bf5363b2aaf31e0b4e3919aa9c07cbf2c8a868c13a6e741107617e3"

# ---- write_policy.py: the predicates and the switch snapshot ---------------------------------------------------

OLD_WRITE_POLICY_IMPORT = "from .tool_helpers import truthy_env\n"
NEW_WRITE_POLICY_IMPORT = (
    "from .tool_helpers import truthy_env\n"
    "\n"
    "from odoo_mcp_admin import write_gates as _woow_write_gates  # PATCH(woowtech): semantics of the write switches\n"
)

OLD_WRITE_POLICY = (
    'def chatter_direct_enabled() -> bool:\n'
    '    """Return True when chatter_post may bypass approval-token gating."""\n'
    '    return truthy_env("MCP_CHATTER_DIRECT")\n'
)
NEW_WRITE_POLICY = OLD_WRITE_POLICY + (
    '\n'
    '\n'
    'def direct_writes_enabled() -> bool:\n'
    '    """PATCH(woowtech): execute_method may run create/write/unlink directly (ODOO_MCP_ALLOW_DIRECT_WRITES).\n'
    '\n'
    '    The one predicate shared by the execute_method guard, its tool text and health_check\'s runtime posture.\n'
    '    """\n'
    '    return os.environ.get("ODOO_MCP_ALLOW_DIRECT_WRITES", "").strip().lower() in ("1", "true", "yes")\n'
    '\n'
    '\n'
    'def write_switches():\n'
    '    """PATCH(woowtech): the four write switches, each read by the predicate that enforces it.\n'
    '\n'
    '    odoo_mcp_admin.write_gates turns this one snapshot into the execute_method / chatter_post decisions, the\n'
    '    texts of the five write tools and the health_check note, so text, decision and posture cannot disagree.\n'
    '    """\n'
    '    return _woow_write_gates.Switches(\n'
    '        enable=writes_enabled(),\n'
    '        direct=direct_writes_enabled(),\n'
    '        unknown=truthy_env("ODOO_MCP_ALLOW_UNKNOWN_METHODS"),\n'
    '        chatter=chatter_direct_enabled(),\n'
    '    )\n'
)

# ---- tools_write.py: decisions and texts come from odoo_mcp_admin.write_gates ----------------------------------

OLD_TOOLS_WRITE_IMPORT = (
    "from .write_policy import chatter_direct_enabled, side_effect_method_allowed, writes_enabled\n"
)
NEW_TOOLS_WRITE_IMPORT = (
    "from .write_policy import (  # PATCH(woowtech): write_switches feeds odoo_mcp_admin.write_gates\n"
    "    chatter_direct_enabled,\n"
    "    side_effect_method_allowed,\n"
    "    write_switches,\n"
    "    writes_enabled,\n"
    ")\n"
    "from odoo_mcp_admin import write_gates  # PATCH(woowtech): semantics of the four write switches\n"
)

_TEXT_COMMENT = "    # PATCH(woowtech): the text follows the write switches (odoo_mcp_admin.write_gates).\n"

OLD_PREVIEW_DESCRIPTION = '    description="Preview create, write, or unlink without executing it",\n'
NEW_PREVIEW_DESCRIPTION = _TEXT_COMMENT + "    description=write_gates.preview_write_text(write_switches()),\n"
OLD_VALIDATE_DESCRIPTION = '    description="Validate a standard write payload against optional fields_get metadata",\n'
NEW_VALIDATE_DESCRIPTION = _TEXT_COMMENT + "    description=write_gates.validate_write_text(write_switches()),\n"
OLD_EXECUTE_APPROVED_DESCRIPTION = '    description="Execute a previously previewed and confirmed standard write",\n'
NEW_EXECUTE_APPROVED_DESCRIPTION = (
    _TEXT_COMMENT + "    description=write_gates.execute_approved_write_text(write_switches()),\n"
)

OLD_CHATTER_DESCRIPTION = (
    '    description=(\n'
    '        "Post a chatter message on a mail.thread record. Default mode requires "\n'
    '        "an approval token returned from a preview call; set MCP_CHATTER_DIRECT=1 "\n'
    '        "to bypass and post immediately."\n'
    '    ),\n'
)
NEW_CHATTER_DESCRIPTION = _TEXT_COMMENT + "    description=write_gates.chatter_post_text(write_switches()),\n"

OLD_METHOD_DESCRIPTION = (
    '            description=(\n'
    '                "Odoo model method to call; direct create, write, and unlink are blocked."\n'
    '            )\n'
)
NEW_METHOD_DESCRIPTION = (
    "            # PATCH(woowtech): the text follows the write switches (odoo_mcp_admin.write_gates).\n"
    "            description=write_gates.execute_method_text(write_switches())\n"
)

OLD_EXECUTE_METHOD_GUARD = (
    '        if method in DESTRUCTIVE_METHODS:\n'
    '            return {\n'
    '                "success": False,\n'
    '                "error": (\n'
    '                    "Direct execute_method blocks create/write/unlink. Use "\n'
    '                    "preview_write -> validate_write -> execute_approved_write."\n'
    '                ),\n'
    '            }\n'
    '        review_required = safety["safety"] in {"side_effect", "unknown"}\n'
    '        if (\n'
    '            review_required\n'
    '            and not side_effect_method_allowed(model, method)\n'
    '            and not truthy_env("ODOO_MCP_ALLOW_UNKNOWN_METHODS")\n'
    '        ):\n'
    '            return {\n'
    '                "success": False,\n'
    '                "error": (\n'
    '                    "Unreviewed side-effect methods are blocked by default. Review "\n'
    '                    "custom source, then add the exact \'model.method\' to the policy "\n'
    '                    "file (ODOO_MCP_POLICY_FILE, default ./odoo_mcp_policy.json, "\n'
    '                    "re-read on every request — see odoo_mcp_policy.json.example) "\n'
    '                    "or to ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS=model.method, or "\n'
    '                    "set ODOO_MCP_ALLOW_UNKNOWN_METHODS=1 only for trusted "\n'
    '                    "deployments."\n'
    '                ),\n'
    '                "classification": safety,\n'
    '            }\n'
)
NEW_EXECUTE_METHOD_GUARD = (
    '        # PATCH(woowtech): one decision for the four write switches (odoo_mcp_admin.write_gates): the master\n'
    '        # switch, direct writes, unreviewed methods and chatter calls; the allowlist is read only when needed.\n'
    '        refusal = write_gates.execute_method_refusal(\n'
    '            model, method, safety, lambda: side_effect_method_allowed(model, method), write_switches()\n'
    '        )\n'
    '        if refusal is not None:\n'
    '            return refusal\n'
)

OLD_CHATTER_ENTRY = (
    '    try:\n'
    '        instance_name, odoo = _resolve_odoo(ctx, instance)\n'
    '        validate_model_name(model)\n'
    '        if record_id < 1:\n'
)
NEW_CHATTER_ENTRY = (
    '    try:\n'
    '        # PATCH(woowtech): ODOO_MCP_ENABLE_WRITES is the master switch; chatter_post posts nothing while it is off.\n'
    '        refusal = write_gates.chatter_post_refusal(write_switches())\n'
    '        if refusal is not None:\n'
    '            return refusal\n'
    '        instance_name, odoo = _resolve_odoo(ctx, instance)\n'
    '        validate_model_name(model)\n'
    '        if record_id < 1:\n'
)

# validate_write answers with an explicit ``valid`` boolean: the verdict of ``success`` (no error-severity issue);
# every early failure (unusable *_from_path, metadata error, empty metadata, exception) is ``valid: false``.
_VALID_FALSE = '"valid": False,  # PATCH(woowtech): validate_write always answers valid true/false\n'
OLD_VALIDATE_FROM_PATH = (
    '                "tool": "validate_write",\n'
    '                "error": (\n'
    '                    "*_from_path uploads require'
)
NEW_VALIDATE_FROM_PATH = (
    '                "tool": "validate_write",\n'
    '                ' + _VALID_FALSE +
    '                "error": (\n'
    '                    "*_from_path uploads require'
)
OLD_VALIDATE_METADATA_ERROR = (
    '                    "tool": "validate_write",\n'
    '                    "error": fields_metadata["error"],\n'
)
NEW_VALIDATE_METADATA_ERROR = (
    '                    "tool": "validate_write",\n'
    '                    ' + _VALID_FALSE +
    '                    "error": fields_metadata["error"],\n'
)
OLD_VALIDATE_METADATA_EMPTY = (
    '                    "tool": "validate_write",\n'
    '                    "error": "live fields_get metadata was empty; refusing to approve writes",\n'
)
NEW_VALIDATE_METADATA_EMPTY = (
    '                    "tool": "validate_write",\n'
    '                    ' + _VALID_FALSE +
    '                    "error": "live fields_get metadata was empty; refusing to approve writes",\n'
)
OLD_VALIDATE_REPORT = (
    '            instance=instance_name,\n'
    '        )\n'
    '        trusted_live_metadata = (\n'
)
NEW_VALIDATE_REPORT = (
    '            instance=instance_name,\n'
    '        )\n'
    '        # PATCH(woowtech): an explicit valid boolean, the same verdict as success (no error-severity issue).\n'
    '        report["valid"] = bool(report.get("success"))\n'
    '        trusted_live_metadata = (\n'
)
OLD_VALIDATE_EXCEPTION = '        return {"success": False, "tool": "validate_write", "error": str(e)}\n'
NEW_VALIDATE_EXCEPTION = (
    '        return {"success": False, "tool": "validate_write", "valid": False, "error": str(e)}  # PATCH(woowtech)\n'
)

# ---- server_core.py: health_check ------------------------------------------------------------------------------

OLD_SERVER_CORE_IMPORT = (
    "from .write_policy import (\n"
    "    allowed_side_effect_methods,\n"
    "    chatter_direct_enabled,\n"
    "    load_side_effect_policy,\n"
    "    writes_enabled,\n"
    ")\n"
)
NEW_SERVER_CORE_IMPORT = (
    "from .write_policy import (\n"
    "    allowed_side_effect_methods,\n"
    "    chatter_direct_enabled,\n"
    "    direct_writes_enabled,  # PATCH(woowtech)\n"
    "    load_side_effect_policy,\n"
    "    write_switches,  # PATCH(woowtech)\n"
    "    writes_enabled,\n"
    ")\n"
    "from odoo_mcp_admin import write_gates  # PATCH(woowtech): semantics of the four write switches\n"
)
OLD_SERVER_CORE_REPORT = '        "write_execution_enabled": writes_enabled(),\n'
NEW_SERVER_CORE_REPORT = (
    '        "write_execution_enabled": writes_enabled(),\n'
    '        # PATCH(woowtech): ODOO_MCP_ALLOW_DIRECT_WRITES, the same predicate as the execute_method guard.\n'
    '        "direct_writes_enabled": direct_writes_enabled(),\n'
)
OLD_SERVER_CORE_BROAD_VAR = '    broad_unknown_enabled = truthy_env("ODOO_MCP_ALLOW_UNKNOWN_METHODS")\n'
NEW_SERVER_CORE_BROAD_VAR = (
    '    broad_unknown_enabled = truthy_env("ODOO_MCP_ALLOW_UNKNOWN_METHODS")\n'
    '    # PATCH(woowtech): the broad mode in effect -- unreviewed methods run directly only when writes, direct writes\n'
    '    # and unknown methods are all on (odoo_mcp_admin.write_gates); unknown_execute_method_enabled stays the switch.\n'
    '    broad_unknown_in_effect = write_gates.unknown_methods_run(write_switches())\n'
)
OLD_SERVER_CORE_BROAD = (
    '            "enabled": broad_unknown_enabled,\n'
    '            "risk": ("broad" if broad_unknown_enabled else "off"),\n'
)
NEW_SERVER_CORE_BROAD = (
    '            "enabled": broad_unknown_in_effect,  # PATCH(woowtech)\n'
    '            "risk": ("broad" if broad_unknown_in_effect else "off"),\n'
)
OLD_SERVER_CORE_NOTE = (
    '            "execute_method blocks standard destructive methods and unreviewed side-effect methods by default.",\n'
)
NEW_SERVER_CORE_NOTE = (
    '            # PATCH(woowtech): this sentence follows the write switches (odoo_mcp_admin.write_gates).\n'
    '            write_gates.runtime_note(write_switches()),\n'
)


def find_tools_write() -> list[str]:
    """Locate tools_write.py inside the installed odoo_mcp package."""
    paths: list[str] = []
    # Try importlib first (most reliable)
    try:
        import importlib.util
        spec = importlib.util.find_spec("odoo_mcp.tools_write")
        if spec and spec.origin:
            paths.append(spec.origin)
    except (ImportError, ModuleNotFoundError, ValueError):
        pass
    # Fallback to glob patterns
    patterns = [
        os.path.join(sys.prefix, "lib", "python*", "site-packages", "odoo_mcp", "tools_write.py"),
        os.path.join(os.path.dirname(__file__), "..", "src", "odoo_mcp", "tools_write.py"),
    ]
    for pattern in patterns:
        for match in glob.glob(pattern):
            if match not in paths:
                paths.append(match)
    return paths


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _transform(name: str, content: str) -> str:
    """Apply this patch's replacements to one file's text (each target exactly once)."""
    replacements = {
        "tools_write.py": ((OLD_TOOLS_WRITE_IMPORT, NEW_TOOLS_WRITE_IMPORT),
                           (OLD_PREVIEW_DESCRIPTION, NEW_PREVIEW_DESCRIPTION),
                           (OLD_VALIDATE_DESCRIPTION, NEW_VALIDATE_DESCRIPTION),
                           (OLD_EXECUTE_APPROVED_DESCRIPTION, NEW_EXECUTE_APPROVED_DESCRIPTION),
                           (OLD_CHATTER_DESCRIPTION, NEW_CHATTER_DESCRIPTION),
                           (OLD_METHOD_DESCRIPTION, NEW_METHOD_DESCRIPTION),
                           (OLD_EXECUTE_METHOD_GUARD, NEW_EXECUTE_METHOD_GUARD),
                           (OLD_CHATTER_ENTRY, NEW_CHATTER_ENTRY),
                           (OLD_VALIDATE_FROM_PATH, NEW_VALIDATE_FROM_PATH),
                           (OLD_VALIDATE_METADATA_ERROR, NEW_VALIDATE_METADATA_ERROR),
                           (OLD_VALIDATE_METADATA_EMPTY, NEW_VALIDATE_METADATA_EMPTY),
                           (OLD_VALIDATE_REPORT, NEW_VALIDATE_REPORT),
                           (OLD_VALIDATE_EXCEPTION, NEW_VALIDATE_EXCEPTION)),
        "write_policy.py": ((OLD_WRITE_POLICY_IMPORT, NEW_WRITE_POLICY_IMPORT),
                            (OLD_WRITE_POLICY, NEW_WRITE_POLICY)),
        "server_core.py": ((OLD_SERVER_CORE_IMPORT, NEW_SERVER_CORE_IMPORT),
                           (OLD_SERVER_CORE_REPORT, NEW_SERVER_CORE_REPORT),
                           (OLD_SERVER_CORE_BROAD_VAR, NEW_SERVER_CORE_BROAD_VAR),
                           (OLD_SERVER_CORE_BROAD, NEW_SERVER_CORE_BROAD),
                           (OLD_SERVER_CORE_NOTE, NEW_SERVER_CORE_NOTE)),
    }[name]
    for old, new in replacements:
        if content.count(old) != 1:
            raise ValueError(f"target block not found exactly once in {name}")
        content = content.replace(old, new, 1)
    return content


# file name -> (accepted input digest, required output digest); tools_write.py first (its error names the release)
_FILES = (
    ("tools_write.py", lambda: (PINNED, PATCHED)),
    ("write_policy.py", lambda: (WRITE_POLICY_PINNED, WRITE_POLICY_PATCHED)),
    ("server_core.py", lambda: (SERVER_CORE_PINNED, SERVER_CORE_PATCHED)),
)


def patch_package(package_dir: str) -> bool:
    """Patch tools_write.py, write_policy.py and server_core.py of one odoo_mcp package, all or nothing.

    Returns True if anything was written, False if all three were already patched; raises ValueError (writing nothing)
    on any unexpected content.
    """
    planned: list[tuple[str, bytes]] = []
    for name, digests in _FILES:
        pinned, patched = digests()
        path = os.path.join(package_dir, name)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            raise ValueError(f"missing {name} in {package_dir}") from None
        digest = _sha256(data)
        if digest == patched:
            continue
        if digest != pinned:
            raise ValueError(f"unexpected {name} (sha256 {digest}); review the new odoo-mcp before patching")
        new = _transform(name, data.decode("utf-8")).encode("utf-8")
        if _sha256(new) != patched:
            raise ValueError(f"patched {name} does not have the reviewed digest")
        planned.append((path, new))
    for path, new in planned:
        with open(path, "wb") as f:
            f.write(new)
    # Clear .pyc cache so Python reads the patched sources
    cache_dir = os.path.join(package_dir, "__pycache__")
    if planned and os.path.isdir(cache_dir):
        for name, _ in _FILES:
            for pyc in glob.glob(os.path.join(cache_dir, name[:-3] + "*.pyc")):
                os.remove(pyc)
                print(f"[patch-direct-writes] Removed cache: {pyc}")
    # Verify
    with open(os.path.join(package_dir, "tools_write.py")) as f:
        assert MARKER in f.read(), "Marker not found after patch"
    if planned:
        print(f"[patch-direct-writes] Applied to {package_dir} ({', '.join(os.path.basename(p) for p, _ in planned)})")
    else:
        print(f"[patch-direct-writes] Already applied to {package_dir}")
    return bool(planned)


def patch(filepath: str) -> bool:
    """Apply the patch to the odoo_mcp package that contains *filepath* (its ``tools_write.py``); see patch_package."""
    return patch_package(os.path.dirname(os.path.abspath(filepath)))


def main() -> int:
    paths = find_tools_write()
    if not paths:
        print("[patch-direct-writes] ERROR: could not locate odoo_mcp/tools_write.py", file=sys.stderr)
        return 1
    ok = True
    for filepath in paths:
        try:
            patch(filepath)
        except Exception as exc:
            print(f"[patch-direct-writes] ERROR: {exc}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
