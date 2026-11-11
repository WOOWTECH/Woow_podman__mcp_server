"""Nextcloud MCP 工具 registry（core ``create_tools_router`` 介面＋子行程 gating 的單一對照）。

工具名稱與分級的正典在 vendored ``nextcloud_mcp_server.tools``（``READ_TOOLS``／``WRITE_TOOLS``／
``DELETE_TOOLS``，子行程依它註冊）；vendored 套件沒有 admin 用的 registry，所以這裡補上 Tools 頁需要的
欄位：``ToolCategory``（可疊代 str-Enum）、``TOOL_REGISTRY``（name／description／category／operations／
dangerous）、``get_all_tool_names()``、``get_tool_by_name()``。名稱一律取自 vendored 常數，
``tests/test_tool_registry.py`` 守住「Tools 頁列出的＝子行程實際註冊的」。

9 支、2 類（files／calendar）、3 級：

- read（5）：get_file_tree、get_file_content、read_text_file、list_calendars、list_tasks
- write（3）：create_text_file、update_text_file、upload_file
- destructive（1）：delete_file_checked（``dangerous``，Tools 頁紅標）

預設 9 支全開（負責人 2026-10-25「預設全開」：新安裝由 ``seed_defaults`` 不停用任何工具；2026-10-08～10-24
開通、出廠只開 read 的服務，第一次以新映像啟動時由 core 的 ``owner_defaults`` 一次性清空停用清單）。政策有開 write／destructive 工具時，launcher 才把 ``NEXTCLOUD_MCP_READONLY=false``／
``NEXTCLOUD_MCP_ALLOW_DELETE=true`` 交給子行程（見 ``launcher.policy_env``）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from mcp_admin_core.operation_policy import OperationSpec

from nextcloud_mcp_server.tools import ALL_TOOLS, DELETE_TOOLS, READ_TOOLS, WRITE_TOOLS

ACCESS_READ = "read"
ACCESS_WRITE = "write"
ACCESS_DESTRUCTIVE = "destructive"

OP_READ = "read"
OP_CREATE = "create"
OP_UPDATE = "update"
OP_DELETE = "delete"


class ToolCategory(str, Enum):
    """Tools 頁分組。"""

    FILES = "files"
    CALENDAR = "calendar"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: ToolCategory
    description: str
    access: str
    operations: tuple[str, ...] = field(default_factory=lambda: (OP_READ,))

    @property
    def dangerous(self) -> bool:
        return self.access == ACCESS_DESTRUCTIVE


_F = ToolCategory.FILES
_C = ToolCategory.CALENDAR

# 管理頁顯示用說明（給租戶看，zh-TW）；給模型看的英文說明在 vendored DESCRIPTIONS。
_SPECS: dict[str, tuple[ToolCategory, tuple[str, ...], str]] = {
    "get_file_tree": (_F, (OP_READ,), "列出資料夾內的檔案與子資料夾（1–3 層，資料夾在前、有數量上限）。"),
    "get_file_content": (_F, (OP_READ,), "以純文字取回一個 UTF-8 文字檔的內容。"),
    "read_text_file": (_F, (OP_READ,), "讀取一個 UTF-8 文字檔，連同 etag、大小、類型（之後更新／刪除要用 etag）。"),
    "list_calendars": (_C, (OP_READ,), "列出帳號可見的行事曆（id、名稱、元件類型、顏色、是否可寫）。"),
    "list_tasks": (_C, (OP_READ,), "讀取行事曆中的待辦事項（VTODO），未完成的在前、依到期日排序。"),
    "create_text_file": (_F, (OP_CREATE,), "建立新的文字檔；檔案已存在時一律失敗，不會覆蓋。"),
    "update_text_file": (_F, (OP_UPDATE,), "改寫文字檔內容；必須帶讀取時的 etag，檔案已被別人改過就拒絕。"),
    "upload_file": (_F, (OP_CREATE, OP_UPDATE), "以 base64 上傳任何檔案：不帶 etag 只能新建，帶 etag 只取代那個版本。"),
    "delete_file_checked": (_F, (OP_DELETE,), "刪除單一檔案（不能刪資料夾），etag 必須相符；檔案會進 Nextcloud 垃圾桶。"),
}


def _access(name: str) -> str:
    if name in READ_TOOLS:
        return ACCESS_READ
    if name in WRITE_TOOLS:
        return ACCESS_WRITE
    if name in DELETE_TOOLS:
        return ACCESS_DESTRUCTIVE
    raise KeyError(name)


TOOL_REGISTRY: tuple[ToolSpec, ...] = tuple(
    ToolSpec(
        name=name,
        category=_SPECS[name][0],
        description=_SPECS[name][2],
        access=_access(name),
        operations=_SPECS[name][1],
    )
    for name in ALL_TOOLS
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_REGISTRY}

if set(_SPECS) != set(ALL_TOOLS):  # pragma: no cover — vendored 工具面變動時立即在 import 期爆出來
    raise RuntimeError(
        f"tool_registry out of sync with vendored nextcloud_mcp_server.tools: "
        f"{sorted(set(_SPECS) ^ set(ALL_TOOLS))}"
    )


def get_tool_by_name(name: str) -> ToolSpec | None:
    return TOOLS_BY_NAME.get(name)


def get_all_tool_names() -> list[str]:
    return [spec.name for spec in TOOL_REGISTRY]


def names_by_access(access: str) -> list[str]:
    return [spec.name for spec in TOOL_REGISTRY if spec.access == access]


def read_only_tool_names() -> list[str]:
    return names_by_access(ACCESS_READ)


def write_tool_names() -> list[str]:
    return names_by_access(ACCESS_WRITE)


def dangerous_tool_names() -> list[str]:
    return names_by_access(ACCESS_DESTRUCTIVE)


def write_and_dangerous_tool_names() -> list[str]:
    """寫入＋破壞性工具（4 支）：停用它們就是唯讀政策（2026-10-25 之前新安裝的出廠政策）。"""
    return write_tool_names() + dangerous_tool_names()


def default_disabled_tools() -> list[str]:
    """新安裝的停用清單：空——9 支全開（負責人 2026-10-25「預設全開」）。"""
    return []


# Reviewed against NextcloudTools.upload_file: None means create; a validated
# expected_etag means replacement. Other handlers perform one fixed operation.
OPERATION_POLICIES = {
    spec.name: (OperationSpec(tuple(spec.operations), "expected_etag", "create",
                              optional_string=("create", "update"))
                if spec.name == "upload_file" else
                OperationSpec(tuple(spec.operations), default=spec.operations[0]))
    for spec in TOOL_REGISTRY
}
