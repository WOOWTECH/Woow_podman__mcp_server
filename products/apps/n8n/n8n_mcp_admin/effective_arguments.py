"""Permission-relevant preprocessing of the pinned n8n-mcp 2.96.2 child.

The child replaces one output wrapper *before* its scalar coercion/dispatch.
Required keys and top-level types come from its actual 28 schemas, not enums
inferred as permissions. Remaining non-policy field validation/coercion belongs
to the child. A second recognizable wrapper is refused so repeated public
checks and the child's own preprocessing cannot select different arguments.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


SCHEMAS = json.loads(Path(__file__).with_name("argument_schemas.json").read_text())["schemas"]
EXPOSURE_TOOLS = frozenset({"n8n_workflow_versions", "n8n_test_workflow"})
# ECMAScript TrimString; Python str.strip differs for BOM and C0 separators.
_JS_SPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_JS_NUMBER = re.compile(r"(?:[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)|0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+)\Z")
MAX_WRAPPER_NESTING = 512


def _check_wrapper_nesting(source: str) -> None:
    """Bound JSON depth independently of Python's version-specific parser limit."""
    depth = 0
    quoted = escaped = False
    for char in source:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_WRAPPER_NESTING:
                raise ValueError("Unverifiable n8n wrapper")
        elif char in "]}":
            depth -= 1


def _typeof(value):
    if value is None or isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if type(value) is bool:
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string" if isinstance(value, str) else None


def _constant(_value):
    # JSON.parse rejects these too, so syntax fallback is equivalent.
    raise json.JSONDecodeError("Invalid JSON constant", "", 0)


def _extract(arguments, schema):
    output = arguments.get("output")
    if not isinstance(output, str) or not output.strip(_JS_SPACE).startswith("{"):
        return None
    _check_wrapper_nesting(output)
    try:
        # JSON.parse receives the original string, not its trimmed copy.
        parsed = json.loads(output, parse_constant=_constant)
    except json.JSONDecodeError:
        return None
    except (ValueError, RecursionError):
        # Python's integer-digit/recursion limits are not JSON syntax errors:
        # JSON.parse may accept the wrapper and select a hidden write.
        raise ValueError("Unverifiable n8n wrapper") from None
    if not isinstance(parsed, dict) or any(key not in parsed for key in schema["required"]):
        return None
    # server.validateExtractedArgs checks types, not enum membership. Numeric
    # strings are accepted for type=number, but NOT type=integer/boolean.
    for key, value in parsed.items():
        expected = schema["types"].get(key)
        if expected and expected != _typeof(value):
            if expected == "number" and isinstance(value, str):
                trimmed = value.strip(_JS_SPACE)
                if not trimmed or _JS_NUMBER.fullmatch(trimmed):
                    continue
            return None
    return parsed


def normalize_arguments(name, arguments):
    """Return a new canonical policy object; never rewrite a denied input in place."""
    if name not in SCHEMAS or not isinstance(arguments, dict):
        raise ValueError("Unrecognized n8n arguments")
    schema = SCHEMAS[name]
    extracted = _extract(arguments, schema)
    effective = dict(arguments if extracted is None else extracted)
    if _extract(effective, schema) is not None:
        raise ValueError("Ambiguous nested n8n arguments")
    if name in EXPOSURE_TOOLS and "exposeToMcp" in effective:
        value = effective["exposeToMcp"]
        if isinstance(value, str):
            trimmed = value.strip(_JS_SPACE)
            if trimmed in ("true", "false"):
                value = trimmed == "true"
        if type(value) is not bool:
            raise ValueError("Invalid exposure flag")
        effective["exposeToMcp"] = value
    return effective


def conditional_call_disabled(name, arguments, disabled_tools, disabled_operations):
    """Exposure is a workflow update even if the primary operation is a read."""
    updater = "n8n_update_partial_workflow"
    return (name in EXPOSURE_TOOLS and arguments.get("exposeToMcp") is True
            and (updater in disabled_tools or bool(disabled_operations.get(updater))))
