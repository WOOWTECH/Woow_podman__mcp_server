"""Three-level tool gating: category, tool, operation, plus read-only mode.

The gate is built from :class:`~woow_litellm_mcp_server.settings.Settings` and is
consulted by each tool module's ``register`` function so disabled tools are
never even registered on the FastMCP server.

Precedence (a tool is enabled only if ALL hold):
  1. its category is not in ``disabled_categories``;
  2. its name is not in ``disabled_tools``;
  3. read-only mode is off OR the tool is not ``dangerous``;
  4. at least one of the tool's declared operations is still allowed.

Rule 4 is what makes the operations column in the admin GUI real. Every tool in
the registry declares exactly the operations it performs, so disabling the only
operation a tool has (or turning on read-only, which disables every non-``read``
operation) removes the tool from the surface entirely — it is never registered
on the FastMCP server, so ``tools/list`` shrinks and ``tools/call`` 404s.

Operation gating additionally filters the operations a still-enabled tool may
perform via ``disabled_operations`` (entries may be ``"tool:op"`` or bare
``"op"``); in read-only mode only the ``read`` operation survives. Mutating
tools additionally call :meth:`ToolGate.require_operation` in their handler
body, so a gate that changes shape between registration and invocation still
refuses the upstream call instead of performing it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .errors import ToolError
from .registry import (
    ACTIVE_OPERATION_TOOLS,
    OP_CREATE,
    OP_READ,
    TOOL_REGISTRY,
    TOOLS_BY_NAME,
    ToolCategory,
    ToolSpec,
)
from .settings import Settings, get_settings


def _normalize_operations(value: Any) -> set[str]:
    """Coerce a disabled-operations spec into a ``{"tool:op", "op"}`` set.

    Two shapes are accepted so both call sites agree:

    * the Settings/env form — a list of strings, each ``"tool:op"`` or bare
      ``"op"`` (``["litellm_delete_key:delete", "delete"]``);
    * the admin GUI form — a mapping ``{tool_name: [op, ...]}`` which is
      expanded into the ``"tool:op"`` string set.
    """
    if not value:
        return set()
    if isinstance(value, Mapping):
        result: set[str] = set()
        for tool, ops in value.items():
            for op in ops or []:
                if str(op).strip():
                    result.add(f"{tool}:{str(op).strip()}")
        return result
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return {str(o).strip() for o in value if str(o).strip()}
    return {str(value).strip()} if str(value).strip() else set()


class ToolGate:
    """Decides which tools/operations are exposed for a given configuration.

    Construct it either from a :class:`Settings` object (the server path) or
    directly from keyword overrides (the admin-console path, which builds a gate
    on the fly from the stored ``tools`` switches). Keyword overrides win over
    the ``settings`` values when both are supplied.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        readonly: bool | None = None,
        disabled_categories: Iterable[str] | None = None,
        disabled_tools: Iterable[str] | None = None,
        disabled_operations: Any = None,
    ) -> None:
        # No settings and no overrides -> fall back to the process singleton.
        if (
            settings is None
            and readonly is None
            and disabled_categories is None
            and disabled_tools is None
            and disabled_operations is None
        ):
            settings = get_settings()

        if settings is not None:
            if readonly is None:
                readonly = settings.readonly
            if disabled_categories is None:
                disabled_categories = settings.disabled_categories
            if disabled_tools is None:
                disabled_tools = settings.disabled_tools
            if disabled_operations is None:
                disabled_operations = settings.disabled_operations

        self.readonly: bool = bool(readonly)
        self._disabled_categories: set[str] = {
            str(c).strip().lower() for c in (disabled_categories or []) if str(c).strip()
        }
        self._disabled_tools: set[str] = {
            str(t).strip() for t in (disabled_tools or []) if str(t).strip()
        }
        self._disabled_operations: set[str] = _normalize_operations(disabled_operations)

    # -- category ----------------------------------------------------------
    def is_category_enabled(self, category: ToolCategory | str) -> bool:
        value = category.value if isinstance(category, ToolCategory) else str(category)
        return value.lower() not in self._disabled_categories

    # -- tool --------------------------------------------------------------
    def is_tool_enabled(self, name: str) -> bool:
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return False
        if not self.is_category_enabled(spec.category):
            return False
        if spec.name in self._disabled_tools:
            return False
        if self.readonly and spec.dangerous:
            return False
        # A tool with no surviving operation is not a tool. This is the check
        # that enforces `disabled_operations` (previously dead code: the GUI's
        # operation switches were decorative) and closes the read-only hole
        # where 15 mutating-but-not-`dangerous` tools stayed live.
        if not self.allowed_operations(name):
            return False
        return True

    # -- operation ---------------------------------------------------------
    def is_operation_allowed(self, name: str, operation: str) -> bool:
        # A policy-label correction must not revive a previously denied tool.
        # Only these three fixed operations have the old read label.
        if name in ACTIVE_OPERATION_TOOLS and operation == OP_CREATE and (
            OP_READ in self._disabled_operations
            or f"{name}:{OP_READ}" in self._disabled_operations
        ):
            return False
        if f"{name}:{operation}" in self._disabled_operations:
            return False
        if operation in self._disabled_operations:
            return False
        if self.readonly and operation != OP_READ:
            return False
        return True

    def allowed_operations(self, name: str) -> tuple[str, ...]:
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return ()
        return tuple(
            op for op in spec.operations if self.is_operation_allowed(name, op)
        )

    def require_operation(self, name: str, operation: str) -> None:
        """Raise unless ``operation`` is allowed for ``name``.

        Called from the body of every mutating tool immediately before it talks
        to LiteLLM. Registration-time gating already hides such tools, so this
        is the second line of defence: it guarantees a gated operation can never
        reach the gateway even if the tool was registered by an older/looser
        gate, and it turns the refusal into an explicit, LLM-readable message
        instead of a silent success.
        """
        if self.is_operation_allowed(name, operation):
            return
        reason = (
            "the server is in read-only mode"
            if self.readonly and operation != OP_READ
            else "it is disabled by the operation policy"
        )
        raise ToolError(
            f"Operation '{operation}' on tool '{name}' is not permitted: "
            f"{reason}. Ask an administrator to enable it in the admin "
            f"console's Tools page, or use a read-only tool instead."
        )

    # -- explanations ------------------------------------------------------
    def explain_disabled(self, name: str) -> str | None:
        """Explain, in one LLM-readable sentence, why ``name`` is not exposed.

        Returns ``None`` when the tool IS exposed, or when the name is not in
        the registry at all (an unknown name is a genuine "unknown tool" and
        must keep the transport's default message).

        This exists because gated tools are *unregistered*, not stubbed: a
        client holding a stale ``tools/list`` that calls one gets FastMCP's
        bare ``Unknown tool: 'litellm_generate_key'``, which reads like the
        server is broken or the name is misspelled rather than like an
        administrator deliberately switched it off. ``GatingMiddleware`` calls
        this on every ``tools/call`` and turns the answer into a ToolError.
        """
        spec = TOOLS_BY_NAME.get(name)
        if spec is None or self.is_tool_enabled(name):
            return None

        hint = (
            " Call tools/list to refresh the currently available tool set, and "
            "ask an administrator to re-enable it on the admin console's Tools "
            "page if you need it."
        )
        category = (
            spec.category.value
            if isinstance(spec.category, ToolCategory)
            else str(spec.category)
        )
        if not self.is_category_enabled(spec.category):
            return (
                f"Tool '{name}' is currently disabled: its whole category "
                f"'{category}' has been switched off by the administrator.{hint}"
            )
        if spec.name in self._disabled_tools:
            return (
                f"Tool '{name}' is currently disabled: it was individually "
                f"switched off by the administrator (or excluded by the "
                f"permission policy).{hint}"
            )
        if self.readonly and spec.dangerous:
            return (
                f"Tool '{name}' is currently disabled: the server is in "
                f"read-only mode and this tool performs destructive changes "
                f"({'/'.join(spec.operations)}). Use a read-only tool "
                f"instead.{hint}"
            )
        if self.readonly:
            return (
                f"Tool '{name}' is currently disabled: the server is in "
                f"read-only mode and every operation this tool performs "
                f"({'/'.join(spec.operations)}) requires write or active-operation permission. Use a read-only "
                f"tool instead.{hint}"
            )
        return (
            f"Tool '{name}' is currently disabled: every operation it performs "
            f"({'/'.join(spec.operations)}) is blocked by the operation "
            f"policy.{hint}"
        )

    # -- bulk views --------------------------------------------------------
    def enabled_tools(self) -> list[ToolSpec]:
        return [s for s in TOOL_REGISTRY if self.is_tool_enabled(s.name)]

    def enabled_tool_names(self) -> list[str]:
        return [s.name for s in self.enabled_tools()]
