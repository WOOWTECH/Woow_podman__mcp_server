"""Published HA OpenDesign 0.2.15 argument contract for the vendored child.

The vendored tool functions stay byte-faithful.  MCP SDK 2.3 uses each Tool's
``parameters`` for tools/list and ``fn_metadata.arg_model`` for dispatch, so
both must be replaced together.  Keep the existing path guard as defense in
depth.  No HA grant, session, backend or read-size policy is copied here.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.shared.exceptions import MCPError
from pydantic import ConfigDict, Field, ValidationError, field_validator

from . import input_policy

HA_SOURCE_COMMIT = "1dd8a2397f63759383493b30960623e5cd663805"
AGENT_ID_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"
PROMPT_MAX = 20_000


class Arguments(ArgModelBase):
    model_config = ConfigDict(strict=True, extra="forbid")


class Args(Arguments):
    pass


class Project(Arguments):
    project_id: str = Field(max_length=128, pattern=input_policy.PROJECT_ID_PATTERN)


class FileInfo(Project):
    file_path: str = Field(min_length=1, max_length=256, pattern=input_policy.FILE_PATH_PATTERN)

    @field_validator("file_path")
    @classmethod
    def relative(cls, value: str) -> str:
        input_policy.require_file_path(value, for_read=False)
        return value


class ReadFile(FileInfo):
    @field_validator("file_path")
    @classmethod
    def not_secret(cls, value: str) -> str:
        input_policy.require_file_path(value, for_read=True)
        return value


def prompt_field():
    return Field(min_length=1, max_length=PROMPT_MAX, description=(
        f"What the OpenDesign agent should do (plain text, at most {PROMPT_MAX} characters)."))


def agent_field():
    return Field(default="claude", pattern=AGENT_ID_PATTERN, description=(
        "OpenDesign agent id from list_agents (e.g. claude, codex, opencode). The agent must be signed in or "
        "configured in OpenDesign."))


class CreateProject(Arguments):
    prompt: str = prompt_field()
    agent_id: str = agent_field()


class SendMessage(Project):
    prompt: str = prompt_field()
    agent_id: str = agent_field()


MODELS: dict[str, type[Arguments]] = {
    "health": Arguments,
    "version": Arguments,
    "list_agents": Arguments,
    "list_connectors": Arguments,
    "list_projects": Arguments,
    "get_project": Project,
    "create_project": CreateProject,
    "delete_project": Project,
    "list_project_files": Project,
    "read_file": ReadFile,
    "get_file_info": FileInfo,
    "send_message": SendMessage,
    "list_runs": Args,
    "list_plugins": Arguments,
    "list_skills": Arguments,
}

_SAFE_ERRORS = frozenset({
    "extra_forbidden", "missing", "string_pattern_mismatch", "string_too_short",
    "string_too_long", "string_type",
})
_FIXED_ERROR = "Value not accepted by this field's rules (see the tool's inputSchema)"


def _argument_errors(model: type[Arguments], error: ValidationError) -> list[dict[str, str]]:
    """Return only model-owned field names and fixed Pydantic messages, never input."""
    fields = set(model.model_fields)
    entries = []
    for detail in error.errors(include_url=False, include_input=False, include_context=False)[:20]:
        location = detail.get("loc", ())
        field = ".".join(str(part) if isinstance(part, int) or isinstance(part, str) and part in fields
                         else "*" for part in location)
        kind = detail.get("type")
        safe = kind in _SAFE_ERRORS
        entries.append({
            "field": field,
            "message": detail["msg"][:300] if safe else _FIXED_ERROR,
            "type": kind if safe else "invalid",
        })
    return entries


class _StrictFuncMetadata(FuncMetadata):
    tool_name: str

    def validate_arguments(self, arguments_to_validate: dict[str, Any]) -> dict[str, Any]:
        try:
            return super().validate_arguments(arguments_to_validate)
        except ValidationError as error:
            # SDK's default ToolError embeds input_value in its text.  HA uses
            # JSON-RPC -32602 and redacts the caller's values and unknown keys.
            raise MCPError(-32602, "Invalid params", {
                "tool": self.tool_name,
                "errors": _argument_errors(self.arg_model, error),
            }) from None


def install_input_contract(server: Any) -> int:
    """Install the same schema and validation on all 15 reviewed child tools."""
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    if set(tools) != set(MODELS):
        raise RuntimeError("OpenDesign tool set changed; review HA input contract")
    installed = 0
    for name, model in MODELS.items():
        tool = tools[name]
        schema = model.model_json_schema()
        metadata = tool.fn_metadata
        if isinstance(metadata, _StrictFuncMetadata):
            if metadata.arg_model is not model or tool.parameters != schema:
                raise RuntimeError(f"OpenDesign {name} contract changed after installation")
            continue
        original = tool.parameters
        if (set(original.get("properties", {})) != set(schema.get("properties", {}))
                or set(original.get("required", ())) != set(schema.get("required", ()))
                or any(original["properties"][key].get("default") != value.get("default")
                       for key, value in schema.get("properties", {}).items())):
            raise RuntimeError(f"OpenDesign {name} arguments changed; review HA input contract")
        tool.fn_metadata = _StrictFuncMetadata(
            arg_model=model,
            output_schema=metadata.output_schema,
            output_model=metadata.output_model,
            wrap_output=metadata.wrap_output,
            tool_name=name,
        )
        tool.parameters = schema
        installed += 1
    return installed
