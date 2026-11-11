"""Per-call, fail-closed reading of the core's saved tool policy; no repair."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from mcp_admin_core.operation_policy import read_policy
from mcp_admin_core.config.store import ConfigUnavailable

from .errors import HeadscaleError
from .registry import OPERATION_POLICIES, normalize_arguments

MAX_POLICY_BYTES = 1024 * 1024


class SavedPolicy:
    def __init__(self, path=None):
        self.path = Path(path if path is not None else os.environ.get("MCP_ADMIN_CONFIG", "/data/config.json"))

    def read(self):
        fd = None
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = os.fstat(fd)
            current = self.path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > MAX_POLICY_BYTES
                    or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError()
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_POLICY_BYTES + 1)
            after = os.fstat(fd)
            if len(raw) > MAX_POLICY_BYTES or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            data = json.loads(raw, object_pairs_hook=unique)
            return read_policy(data["tools"])
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ConfigUnavailable):
            raise HeadscaleError("HEADSCALE_POLICY_UNAVAILABLE") from None
        finally:
            if fd is not None:
                os.close(fd)

    def allows(self, name, arguments, policy=None):
        args = normalize_arguments(name, arguments)
        disabled, operations = self.read() if policy is None else policy
        spec = OPERATION_POLICIES[name]
        requested = spec.resolve(args)
        return name not in disabled and requested is not None and not requested & spec.denied(operations.get(name, []))

    def require(self, name, arguments):
        if not self.allows(name, arguments):
            raise HeadscaleError("HEADSCALE_POLICY_DENIED")

