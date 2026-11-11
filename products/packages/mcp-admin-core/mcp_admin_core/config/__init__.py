"""Configuration store for MCP Admin — file-based or K8s-backed."""

from .store import ConfigStore, get_config_store

__all__ = ["ConfigStore", "get_config_store"]
