"""Product registry combines native tasks with the explicitly bound PaaS extension."""
from enum import Enum
from types import SimpleNamespace

from headscale_mcp_server import registry as native
from . import platform

ToolCategory = Enum("ToolCategory", {**{t.name: t.value for t in native.ToolCategory}, "PLATFORM": "PaaS services"})
TOOL_REGISTRY = (*native.TOOL_REGISTRY, SimpleNamespace(name=platform.NAME, category=ToolCategory.PLATFORM,
    description=platform.DESCRIPTION, operations=platform.OPERATIONS, dangerous=True, access="write"))
TOOLS_BY_NAME = {t.name: t for t in TOOL_REGISTRY}
OPERATION_POLICIES = {**native.OPERATION_POLICIES, platform.NAME: platform.POLICY}
OPERATION_NOTES = {**native.OPERATION_NOTES, platform.NAME: "PaaS credentials and fixed workspace/control-service binding are required. Native Headscale API management remains available without this extension."}


def get_all_tool_names():
    return list(TOOLS_BY_NAME)


def get_tool_by_name(name):
    return TOOLS_BY_NAME.get(name)


def normalize_arguments(name, arguments):
    return platform.normalize_arguments(arguments) if name == platform.NAME else native.normalize_arguments(name, arguments)


def input_schema(name):
    return platform.input_schema() if name == platform.NAME else native.input_schema(name)
