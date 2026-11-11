"""The registry <-> tools invariant, plus a tools/list smoke test.

Every tool declared in :mod:`woow_litellm_mcp_server.registry` must be
registered by exactly one tool module, and every registered tool must be
declared in the registry. If these two drift apart the admin GUI (which reads
the registry) will name tools that do not exist, or vice-versa.
"""

from __future__ import annotations

import pytest

from woow_litellm_mcp_server.registry import (
    TOOL_REGISTRY,
    ToolCategory,
    all_tool_names,
)

from .conftest import FakeMCP, build_gate, register_all


def test_registered_names_match_registry_exactly() -> None:
    mcp = register_all(build_gate())
    registered = set(mcp.tools)
    declared = set(all_tool_names())

    missing = declared - registered
    extra = registered - declared
    assert not missing, f"declared but never registered: {sorted(missing)}"
    assert not extra, f"registered but not in the registry: {sorted(extra)}"


def test_surface_size_is_pinned() -> None:
    # Guard against accidental additions/removals of the tool surface.
    # 40, not 38: litellm_plugin_info + litellm_delete_plugin were added because
    # without a delete tool a plugin registered over MCP could never be removed.
    assert len(TOOL_REGISTRY) == 40
    assert len(all_tool_names()) == len(set(all_tool_names())), "duplicate tool name"


def test_every_tool_name_is_prefixed() -> None:
    for name in all_tool_names():
        assert name.startswith("litellm_"), name


def test_every_registered_tool_has_annotations() -> None:
    mcp = register_all(build_gate())
    for name in mcp.tools:
        assert mcp.annotations.get(name) is not None, name


def test_every_category_has_at_least_one_tool() -> None:
    covered = {spec.category for spec in TOOL_REGISTRY}
    assert covered == set(ToolCategory)


def test_registry_paths_and_methods_are_sane() -> None:
    for spec in TOOL_REGISTRY:
        assert spec.method in {"GET", "POST", "PUT", "PATCH", "DELETE"}, spec.name
        assert spec.path.startswith("/"), spec.name
        assert spec.operations, spec.name


async def test_build_server_exposes_the_full_surface() -> None:
    """Smoke test: the real FastMCP server lists every registered tool."""
    fastmcp = pytest.importorskip("fastmcp")
    from woow_litellm_mcp_server.server import build_server

    server = build_server(build_gate())
    assert server is not None

    # FastMCP's introspection API has shifted across versions; try the known
    # shapes (3.x list_tools(), 2.x get_tools(), and the private manager) and
    # skip (rather than fail) if none is available.
    def _names(tools) -> set[str]:
        if isinstance(tools, dict):
            return set(tools.keys())
        return {t.name if not isinstance(t, str) else t for t in tools}

    names: set[str] = set()
    if hasattr(server, "list_tools"):
        names = _names(await server.list_tools())
    elif hasattr(server, "get_tools"):
        names = _names(await server.get_tools())
    elif hasattr(server, "_tool_manager"):
        store = getattr(server._tool_manager, "_tools", None)
        if isinstance(store, dict):
            names = set(store.keys())

    if not names:
        pytest.skip("FastMCP tool introspection API not available in this version")

    assert names == set(all_tool_names())
