"""tools router factory 所需的 registry 結構協定（typing/文件用，非執行期強制）。"""
from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable


@runtime_checkable
class ToolDef(Protocol):
    name: str
    description: str
    operations: list[str]
    dangerous: bool
    # category 為具 .value 的 str-Enum 成員


@runtime_checkable
class ToolRegistry(Protocol):
    TOOL_REGISTRY: Iterable
    ToolCategory: Iterable

    def get_all_tool_names(self) -> list[str]: ...
    def get_tool_by_name(self, name: str): ...
