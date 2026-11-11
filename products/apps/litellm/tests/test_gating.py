"""Three-level gating: category, tool, operation — plus read-only mode."""

from __future__ import annotations

import pytest

from woow_litellm_mcp_server.errors import ToolError
from woow_litellm_mcp_server.gating import ToolGate
from woow_litellm_mcp_server.registry import (
    OP_READ,
    TOOL_REGISTRY,
    TOOLS_BY_NAME,
    ToolCategory,
)
from woow_litellm_mcp_server.settings import Settings

from .conftest import build_gate, register_all


def _dangerous_names() -> set[str]:
    return {s.name for s in TOOL_REGISTRY if s.dangerous}


def test_default_gate_enables_everything(all_enabled_gate: ToolGate) -> None:
    enabled = set(all_enabled_gate.enabled_tool_names())
    assert enabled == {s.name for s in TOOL_REGISTRY}


def test_disable_category_drops_the_whole_family() -> None:
    gate = build_gate(disabled_categories=["keys"])
    enabled = set(gate.enabled_tool_names())
    key_tools = {s.name for s in TOOL_REGISTRY if s.category is ToolCategory.KEYS}
    assert key_tools, "sanity: there are key tools"
    assert not (enabled & key_tools)
    # Other categories are untouched.
    assert "litellm_list_models" in enabled


def test_disable_single_tool() -> None:
    gate = build_gate(disabled_tools=["litellm_delete_key"])
    assert not gate.is_tool_enabled("litellm_delete_key")
    # Its siblings survive.
    assert gate.is_tool_enabled("litellm_generate_key")


def test_readonly_drops_every_dangerous_tool() -> None:
    gate = build_gate(readonly=True)
    enabled = set(gate.enabled_tool_names())
    dangerous = _dangerous_names()
    assert dangerous, "sanity: the surface has dangerous tools"
    assert not (enabled & dangerous)
    # Read tools remain.
    assert "litellm_list_models" in enabled
    assert "litellm_health_readiness" in enabled
    assert "litellm_health" not in enabled


def test_readonly_registration_never_installs_dangerous_tools() -> None:
    """The gate must stop dangerous tools from ever reaching FastMCP."""
    mcp = register_all(build_gate(readonly=True))
    installed = set(mcp.tools)
    assert not (installed & _dangerous_names())


def test_operation_gate_bare_op() -> None:
    gate = build_gate(disabled_operations=["delete"])
    # A delete tool's delete op is filtered out of its allowed operations.
    assert "delete" not in gate.allowed_operations("litellm_delete_key")


def test_operation_gate_tool_scoped_op() -> None:
    gate = build_gate(disabled_operations=["litellm_generate_key:create"])
    assert "create" not in gate.allowed_operations("litellm_generate_key")
    # A different create tool is unaffected.
    assert "create" in gate.allowed_operations("litellm_create_team")


def test_operation_gate_accepts_dict_form() -> None:
    """The admin GUI passes {tool: [ops]}; the gate must accept that too."""
    gate = ToolGate(disabled_operations={"litellm_update_key": ["update"]})
    assert "update" not in gate.allowed_operations("litellm_update_key")


def test_readonly_leaves_only_read_operations() -> None:
    gate = build_gate(readonly=True)
    for spec in TOOL_REGISTRY:
        if not gate.is_tool_enabled(spec.name):
            continue
        allowed = gate.allowed_operations(spec.name)
        assert all(op == OP_READ for op in allowed), spec.name


def test_unknown_tool_is_never_enabled() -> None:
    gate = build_gate()
    assert not gate.is_tool_enabled("litellm_does_not_exist")
    assert "litellm_does_not_exist" not in TOOLS_BY_NAME


# --- rule 4: operation gates are actually enforced -------------------------
def test_disabling_a_tools_only_operation_removes_the_tool() -> None:
    """`disabled_operations` used to be decorative: parsed, then ignored.

    A tool whose every declared operation is disabled must disappear from the
    surface, not stay registered and happily perform the disabled operation.
    """
    gate = build_gate(disabled_operations=["litellm_delete_key:delete"])
    assert gate.allowed_operations("litellm_delete_key") == ()
    assert not gate.is_tool_enabled("litellm_delete_key")
    assert "litellm_delete_key" not in register_all(gate).tools
    # A sibling with a different operation is untouched.
    assert gate.is_tool_enabled("litellm_generate_key")


def test_readonly_removes_every_mutating_tool_not_just_dangerous_ones() -> None:
    """Read-only leaves only `read` operations, so nothing mutating survives."""
    gate = build_gate(readonly=True)
    for name in gate.enabled_tool_names():
        assert TOOLS_BY_NAME[name].operations == (OP_READ,), name
    assert not gate.is_tool_enabled("litellm_update_key")  # mutating, not dangerous


def test_require_operation_refuses_at_call_time() -> None:
    """Second line of defence inside the tool body, with a readable reason."""
    gate = build_gate(disabled_operations=["update"])
    gate.require_operation("litellm_key_info", OP_READ)  # allowed -> no raise
    with pytest.raises(ToolError) as exc:
        gate.require_operation("litellm_update_key", "update")
    assert "litellm_update_key" in str(exc.value)
    assert "operation policy" in str(exc.value)

    ro = build_gate(readonly=True)
    with pytest.raises(ToolError) as exc:
        ro.require_operation("litellm_update_key", "update")
    assert "read-only" in str(exc.value)


# --- Settings env parsing is what feeds the gate in production -------------
def test_settings_parses_json_and_csv_env_forms() -> None:
    """The admin writes JSON; humans write CSV. Both must reach the gate."""
    json_cfg = Settings(
        disabled_tools='["litellm_delete_key", "litellm_block_key"]',
        disabled_categories='["chat"]',
        disabled_operations='{"litellm_update_key": ["update"]}',
    )
    assert json_cfg.disabled_tools == ["litellm_delete_key", "litellm_block_key"]
    assert json_cfg.disabled_categories == ["chat"]
    assert ToolGate(json_cfg).allowed_operations("litellm_update_key") == ()

    csv_cfg = Settings(
        disabled_tools="litellm_delete_key, litellm_block_key",
        disabled_operations="delete, litellm_update_key:update",
    )
    assert csv_cfg.disabled_tools == ["litellm_delete_key", "litellm_block_key"]
    assert not ToolGate(csv_cfg).is_tool_enabled("litellm_delete_team")


def test_settings_degrades_bad_values_instead_of_raising() -> None:
    """A raise here kills the MCP child process at import time.

    pydantic-settings would json-decode these fields itself and explode on a
    malformed value before our validator ever ran; NoDecode + this validator
    turn "operator typo" into "no switches applied" instead of "server dead".
    """
    cfg = Settings(
        disabled_tools="[broken",
        disabled_categories='{"not": ',
        disabled_operations="[oops",
    )
    assert cfg.disabled_tools == []
    assert cfg.disabled_categories == []
    assert cfg.disabled_operations == []
    assert set(ToolGate(cfg).enabled_tool_names()) == {s.name for s in TOOL_REGISTRY}


def test_settings_accepts_already_parsed_values() -> None:
    cfg = Settings(
        disabled_tools=["litellm_delete_key"],
        disabled_operations={"litellm_update_key": ["update"]},
    )
    assert cfg.disabled_tools == ["litellm_delete_key"]
    assert cfg.disabled_operations == {"litellm_update_key": ["update"]}
    gate = ToolGate(cfg)
    assert not gate.is_tool_enabled("litellm_delete_key")
    assert gate.allowed_operations("litellm_update_key") == ()


def test_settings_env_round_trip(monkeypatch) -> None:
    """The exact env shape the admin writes into the child process."""
    monkeypatch.setenv("LITELLM_MCP_READONLY", "true")
    monkeypatch.setenv("LITELLM_MCP_DISABLED_TOOLS", '["litellm_list_models"]')
    monkeypatch.setenv("LITELLM_MCP_DISABLED_CATEGORIES", '["chat"]')
    monkeypatch.setenv(
        "LITELLM_MCP_DISABLED_OPERATIONS", '{"litellm_key_info": ["read"]}'
    )
    cfg = Settings()
    assert cfg.readonly is True
    assert cfg.disabled_tools == ["litellm_list_models"]
    gate = ToolGate(cfg)
    assert not gate.is_tool_enabled("litellm_list_models")
    assert not gate.is_tool_enabled("litellm_key_info")


def test_settings_singleton_is_lazy() -> None:
    """`settings` is materialised on attribute access, not at import.

    Building it at import time meant any env/validation problem crashed the
    child before FastMCP could report anything.
    """
    import woow_litellm_mcp_server.settings as settings_mod

    assert "settings" not in vars(settings_mod)
    assert isinstance(settings_mod.settings, Settings)
