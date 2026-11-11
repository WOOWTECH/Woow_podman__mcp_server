"""A gated tool must explain itself, not answer "Unknown tool".

Gated tools are unregistered rather than stubbed, so a client holding a cached
``tools/list`` that calls one used to get FastMCP's bare
``Unknown tool: 'litellm_generate_key'`` — which reads as "no such tool exists"
and sends a model hunting for a different name instead of telling it an
administrator switched the tool off.

These tests pin both halves of the contract:

* every registry tool that the gate hides produces a reason from
  :meth:`ToolGate.explain_disabled`, naming the gating dimension responsible;
* a name that is genuinely not in the registry still gets the default
  unknown-tool error, because there it is the correct answer.

The end-to-end case drives a real :class:`fastmcp.Client` against a real
``build_server`` so it also covers the middleware actually being installed and
its ``ToolError`` surviving ``mask_error_details=True``.
"""

from __future__ import annotations

import pytest

from woow_litellm_mcp_server.gating import ToolGate
from woow_litellm_mcp_server.registry import TOOL_REGISTRY, ToolCategory

from .conftest import build_gate


def test_enabled_tool_has_no_explanation(all_enabled_gate: ToolGate) -> None:
    for spec in TOOL_REGISTRY:
        assert all_enabled_gate.explain_disabled(spec.name) is None


def test_unknown_name_is_not_explained(all_enabled_gate: ToolGate) -> None:
    """Not-in-registry must stay "unknown tool" — inventing a reason would lie."""
    assert all_enabled_gate.explain_disabled("litellm_no_such_tool") is None
    assert all_enabled_gate.explain_disabled("") is None


def test_every_hidden_tool_explains_itself() -> None:
    """No gate configuration may leave a hidden registry tool unexplained."""
    gates = [
        build_gate(readonly=True),
        build_gate(disabled_categories=[c.value for c in ToolCategory]),
        build_gate(disabled_tools=[s.name for s in TOOL_REGISTRY]),
        build_gate(
            disabled_operations={s.name: list(s.operations) for s in TOOL_REGISTRY}
        ),
    ]
    for gate in gates:
        hidden = [
            s.name for s in TOOL_REGISTRY if not gate.is_tool_enabled(s.name)
        ]
        assert hidden, "sanity: this gate hides something"
        for name in hidden:
            reason = gate.explain_disabled(name)
            assert reason, f"{name} hidden with no explanation"
            assert name in reason
            assert "disabled" in reason.lower()
            # It must point the caller somewhere useful, not just say no.
            assert "tools/list" in reason


@pytest.mark.parametrize(
    ("kwargs", "tool", "needle"),
    [
        ({"disabled_categories": ["keys"]}, "litellm_list_keys", "category 'keys'"),
        ({"disabled_tools": ["litellm_list_models"]}, "litellm_list_models",
         "individually"),
        ({"readonly": True}, "litellm_delete_key", "read-only mode"),
        ({"readonly": True}, "litellm_generate_key", "read-only mode"),
        ({"disabled_operations": {"litellm_health": ["read"]}}, "litellm_health",
         "operation policy"),
    ],
)
def test_explanation_names_the_responsible_dimension(
    kwargs: dict, tool: str, needle: str
) -> None:
    gate = build_gate(**kwargs)
    assert not gate.is_tool_enabled(tool)
    reason = gate.explain_disabled(tool)
    assert reason is not None and needle in reason


async def test_call_of_gated_tool_returns_the_reason_over_mcp() -> None:
    """End to end: middleware installed, ToolError text survives masking."""
    fastmcp = pytest.importorskip("fastmcp")
    from woow_litellm_mcp_server.server import build_server

    server = build_server(build_gate(readonly=True))
    async with fastmcp.Client(server) as client:
        exposed = {t.name for t in await client.list_tools()}
        assert "litellm_generate_key" not in exposed, "sanity: it is gated off"

        with pytest.raises(Exception) as excinfo:
            await client.call_tool("litellm_generate_key", {})
        message = str(excinfo.value)
        assert "Unknown tool" not in message
        assert "read-only mode" in message
        assert "litellm_generate_key" in message

        # A name that really does not exist keeps the default error.
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("litellm_no_such_tool", {})
        assert "Unknown tool" in str(excinfo.value)
