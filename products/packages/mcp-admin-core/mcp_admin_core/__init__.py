"""MCP Admin Core -- shared library for MCP Admin GUI applications."""

__version__ = "0.1.0"

from .app import create_app
from .config import ConfigStore, get_config_store
from .launch_policy import LaunchPolicy
from .process import McpProcessManager, get_process_manager

__all__ = [
    "ConfigStore",
    "LaunchPolicy",
    "McpProcessManager",
    "create_app",
    "get_config_store",
    "get_process_manager",
]
