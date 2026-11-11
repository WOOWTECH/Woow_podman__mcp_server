"""Three-level tool gate: category / tool / operation."""

from __future__ import annotations

from .registry import TOOL_REGISTRY, TOOLS_BY_NAME, ToolSpec


class ToolGate:
    """Decides which tools may be registered and which operations they keep."""

    def __init__(
        self,
        disabled_categories: list[str] | None = None,
        disabled_tools: list[str] | None = None,
        disabled_operations: dict[str, list[str]] | None = None,
        readonly: bool = False,
    ) -> None:
        self._readonly = readonly
        self._disabled_categories = set(disabled_categories or ())
        self._disabled_tools = set(disabled_tools or ())
        self._disabled_operations = {
            tool: set(ops) for tool, ops in (disabled_operations or {}).items()
        }

    def allowed_operations(self, name: str) -> set[str]:
        """Operations this tool may still perform after gating."""
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return set()
        return set(spec.operations) - self._disabled_operations.get(name, set())

    def is_tool_enabled(self, name: str) -> bool:
        if name in self._disabled_tools:
            return False
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return True
        if self._readonly and spec.dangerous:
            return False
        if spec.category.value in self._disabled_categories:
            return False
        if spec.operations and not self.allowed_operations(name):
            return False
        return True

    def enabled_tools(self) -> list[ToolSpec]:
        return [s for s in TOOL_REGISTRY if self.is_tool_enabled(s.name)]
