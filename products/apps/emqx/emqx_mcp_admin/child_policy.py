"""Live, fail-closed saved policy at the child boundary; never creates config."""
import json
import os
from pathlib import Path
import stat

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from mcp_admin_core.operation_policy import read_policy

from . import tool_registry as registry


class SavedPolicy:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        fd = None
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            s = os.fstat(fd)
            current = self.path.lstat()
            if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.geteuid()
                    or stat.S_IMODE(s.st_mode) != 0o600 or s.st_size > 1024 * 1024
                    or (s.st_dev, s.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError("Unsafe policy file")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as f:
                text = f.read(1024 * 1024 + 1)
            if len(text) > 1024 * 1024:
                raise ValueError("Oversize policy")
            data = json.loads(text)
            return read_policy(data["tools"])
        except Exception:
            raise ToolError("EMQX_POLICY_UNAVAILABLE: saved policy cannot be read safely") from None
        finally:
            if fd is not None:
                os.close(fd)

    def allows(self, name, arguments, policy):
        disabled, operations = policy
        try:
            arguments = registry.normalize_arguments(name, arguments)
            spec = registry.OPERATION_POLICIES[name]
            requested = spec.resolve(arguments)
            return name not in disabled and requested is not None and not requested & spec.denied(operations.get(name, []))
        except (ValueError, KeyError):
            return False


class PolicyMiddleware(Middleware):
    def __init__(self, path):
        self.policy = SavedPolicy(path)

    async def on_call_tool(self, context, call_next):
        if not self.policy.allows(context.message.name, context.message.arguments or {}, self.policy.read()):
            raise ToolError("EMQX_POLICY_DENIED: tool or operation is disabled or invalid")
        return await call_next(context)

    async def on_list_tools(self, context, call_next):
        disabled, operations = self.policy.read()
        tools = await call_next(context)
        return [t for t in tools if t.name in registry.TOOLS_BY_NAME and t.name not in disabled
                and set(registry.OPERATION_POLICIES[t.name].operations)
                - registry.OPERATION_POLICIES[t.name].denied(operations.get(t.name, []))]
