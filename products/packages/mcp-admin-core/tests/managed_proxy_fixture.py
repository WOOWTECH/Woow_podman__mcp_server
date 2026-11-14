"""Represent an explicitly owned test child in the proxy's manager.

The local HTTP/SSE fixtures start their own server; unlike production the
server is not launched by McpProcessManager. This adapter supplies a stable
synthetic process identity, and is not evidence of OS-level peer ownership.

Since blocker 3 the gateway forwards only to a generation whose listener passed
the manager's OS identity check (``McpProcessManager.forward_generation``). A
test-started server has no such check, so :func:`trust_listener` stands in for it
explicitly; production code has no equivalent bypass.
"""
import os
from types import SimpleNamespace

from mcp_admin_core.process import McpProcessManager


class SyntheticProcess:
    """Stands in for a child process; signals only change ``returncode`` (no OS process is signalled)."""

    def __init__(self, pid=None):
        self.pid = os.getpid() if pid is None else pid
        self.returncode = None
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = -int(sig)

    def kill(self):
        self.send_signal(9)

    async def wait(self):
        return self.returncode


def synthetic_manager(process=None, port=None):
    """A manager whose running synthetic child passed the (synthetic) identity check."""
    manager = McpProcessManager()
    manager._process = process or SyntheticProcess()
    manager._running = True
    return trust_listener(manager, port)


def attach(monkeypatch, process=None, port=None):
    from mcp_admin_core import proxy
    manager = synthetic_manager(process or SimpleNamespace(pid=os.getpid(), returncode=None), port)
    monkeypatch.setattr(proxy, 'get_process_manager', lambda: manager)
    return manager


def trust_listener(manager, port=None):
    """Mark *manager*'s current generation as identity-verified (synthetic stand-in for the probe's check).

    ``port=None`` trusts the loopback port the test configured (the one the gateway asks about), and keeps
    trusting a replacement generation the test installs later; an invalidated generation stays unusable.
    """
    if port is not None:
        manager._peer_verified = (manager._initialize_generation_changed, port)
        return manager
    check = McpProcessManager.forward_generation.__get__(manager)

    def forward_generation(requested):
        if manager._peer_verified is None and not manager._initialize_generation_changed.is_set():
            manager._peer_verified = (manager._initialize_generation_changed, requested)
        return check(requested)

    manager.forward_generation = forward_generation
    return manager
