"""OpenDesign-only input guard, installed around the byte-faithful vendored tools.

File-path, secret-name and project-ID rules match the published HA 0.2.15
``od_rules`` overlay, including safe imported non-UUID project IDs.
Validation happens in the child before any request to the OpenDesign daemon;
the separate ``input_contract`` also validates arguments and declares these rules.
"""

from __future__ import annotations

import fnmatch
import functools
import inspect
import re
from typing import Any

PROJECT_ID_PATTERN = r"^\.*[A-Za-z0-9_-][A-Za-z0-9._-]*$"
FILE_PATH_PATTERN = r"^[A-Za-z0-9_㐀-鿿. /-]{1,256}$"
SAFE_PROJECT_ID = re.compile(PROJECT_ID_PATTERN)
FILE_PATH = re.compile(FILE_PATH_PATTERN)
SECRET_PATTERNS = (
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", "*.gpg", "*.asc", "*.ovpn",
    "id_*", "*.env", "env.*", "*credential*", "*secret*", "*password*", "*passwd*",
    "*private*key*", "*apikey*", "*api_key*", "*api-key*", "htpasswd", "shadow",
    "authorized_keys", "known_hosts",
)
PROJECT_TOOLS = frozenset({
    "get_project", "delete_project", "list_project_files", "read_file", "get_file_info", "send_message",
})
FILE_TOOLS = frozenset({"read_file", "get_file_info"})
EXPECTED_TOOLS = frozenset({
    "health", "version", "list_agents", "list_connectors", "list_projects", "get_project", "create_project",
    "delete_project", "list_project_files", "read_file", "get_file_info", "send_message", "list_runs",
    "list_plugins", "list_skills",
})


def require_project_id(value: Any) -> None:
    """Reject URL-shaping IDs; retain UUID and imported safe-ID spelling."""
    if not isinstance(value, str) or len(value) > 128 or SAFE_PROJECT_ID.fullmatch(value) is None:
        raise ValueError("unsafe project_id refused")


def require_file_path(value: Any, *, for_read: bool) -> None:
    """Require an HA-compatible relative path; restrict secret names on reads."""
    if not isinstance(value, str) or FILE_PATH.fullmatch(value) is None:
        raise ValueError("file_path characters not allowed")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("relative non-traversing file_path required")
    if for_read and any(
        part.casefold().startswith(".")
        or any(fnmatch.fnmatchcase(part.casefold(), pattern) for pattern in SECRET_PATTERNS)
        for part in parts
    ):
        raise ValueError("secret-like file_path refused")


def install_input_guards(server: Any) -> int:
    """Guard the six path-bearing tools before their original function can run.

    The MCP SDK keeps each tool's prebuilt ``fn_metadata`` and input schema.
    Fail closed if the vendored tool signatures change or acquire another
    path-bearing tool; a re-sync then needs an explicit policy review.
    """
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    if set(tools) != EXPECTED_TOOLS:
        raise RuntimeError("OpenDesign tool set changed; review input policy")
    count = 0
    for name, tool in tools.items():
        signature = inspect.signature(tool.fn)
        has_project = "project_id" in signature.parameters
        has_file = "file_path" in signature.parameters
        if has_project != (name in PROJECT_TOOLS) or has_file != (name in FILE_TOOLS):
            raise RuntimeError("OpenDesign tool arguments changed; review input policy")
        if name not in PROJECT_TOOLS or getattr(tool.fn, "__od_input_guarded__", False):
            continue
        original = tool.fn

        @functools.wraps(original)
        def guarded(*args: Any, __fn=original, __name=name, __sig=signature, **kwargs: Any) -> Any:
            arguments = __sig.bind(*args, **kwargs).arguments
            require_project_id(arguments["project_id"])
            if __name in FILE_TOOLS:
                require_file_path(arguments["file_path"], for_read=__name == "read_file")
            return __fn(*args, **kwargs)

        guarded.__od_input_guarded__ = True
        tool.fn = guarded
        count += 1
    return count
