"""The outage these cover actually happened in production.

The console kept serving its login page and ``/healthz`` kept answering
``200 {"status":"ok"}`` while the supervised MCP child was dead and every
``/private_{token}/…`` request came back ``Connection refused``. Two separate
defects lined up to produce it:

* ``_drain_logs`` noticed the exit, wrote one WARNING and returned — nothing
  ever restarted the child;
* ``/healthz`` was a literal ``{"status": "ok"}`` that never looked at the
  child, so the container healthcheck was a false green.

Either one alone is survivable. Together they turn a transient crash into a
silent, permanent outage. These tests pin both fixes.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from mcp_admin_core import process as process_mod
from mcp_admin_core.process import McpProcessManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _script_child(body: str) -> dict:
    """An ``mcp_server`` config whose child is an inline python snippet."""
    return {"command": sys.executable, "args": ["-u", "-c", body], "port": 0, "env": {}}


class _StubStore:
    """Minimal stand-in for the async config store used by ``start()``."""

    def __init__(self, mcp_server: dict) -> None:
        self._data = {"mcp_server": mcp_server, "connection": {}}

    async def get(self, key, default=None):
        return self._data.get(key, default)


@pytest.fixture
def fast_backoff(monkeypatch):
    """Keep the supervisor's timing out of the test's wall clock.

    Note that ``_READY_TIMEOUT`` is deliberately NOT patched here: it is bound
    as a default argument on ``wait_ready``, so it is captured at definition
    time and a ``monkeypatch.setattr`` on the module constant does nothing.
    Leaving the no-op patch in would read like the timeout was under control
    when it was not. Tests that must not wait it out use a child that binds
    nothing (``port: 0``), so ``is_ready()`` short-circuits.
    """
    monkeypatch.setattr(process_mod, "_RESTART_BACKOFF_START", 0.05)
    monkeypatch.setattr(process_mod, "_RESTART_BACKOFF_MAX", 0.05)


@pytest.fixture
def patched_store(monkeypatch):
    def _install(cfg: dict):
        monkeypatch.setattr(process_mod, "get_config_store", lambda: _StubStore(cfg))

    return _install


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------


async def test_crashed_child_is_restarted(fast_backoff, patched_store) -> None:
    """An unrequested exit must bring the child back.

    Before the fix this test hung at ``restart_count == 0`` forever: the
    drainer logged the exit and gave up, which is exactly what happened in
    production.
    """
    patched_store(_script_child("import sys; sys.exit(1)"))
    manager = McpProcessManager()
    manager._backoff = 0.05

    assert await manager.start() is True
    # Child exits immediately; the supervisor should keep re-launching it.
    for _ in range(100):
        await asyncio.sleep(0.05)
        if manager._restart_count >= 2:
            break

    assert manager._restart_count >= 2, "supervisor never restarted the dead child"

    await manager.stop()


async def test_stop_is_not_undone_by_the_supervisor(fast_backoff, patched_store) -> None:
    """An operator-requested stop must stay stopped.

    The obvious implementation — restart whenever the child exits — fights the
    operator: ``stop()`` kills the process, the drainer sees the exit and
    dutifully starts it again. ``stop()`` therefore disarms supervision
    *before* it signals.
    """
    patched_store(_script_child("import time; time.sleep(60)"))
    manager = McpProcessManager()

    assert await manager.start() is True
    await asyncio.sleep(0.2)
    await manager.stop()

    count_at_stop = manager._restart_count
    await asyncio.sleep(0.4)

    assert manager.is_running is False, "child came back after an explicit stop"
    assert manager._restart_count == count_at_stop


async def test_superseded_child_does_not_trigger_a_restart() -> None:
    """A stale drainer must not resurrect anything.

    When an operator restart races a crash, the old child's drainer reaches EOF
    while ``self._process`` already points at the *new* child. Supervising on
    that stale reference would spawn a duplicate on top of a healthy process,
    so the drainer is handed its own child and compares identity.

    The child is spawned directly rather than through ``start()``: ``start()``
    attaches its own drainer to the same stdout, and two readers on one stream
    make the assertion depend on which one wins.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        "import sys; sys.exit(3)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    manager = McpProcessManager()
    manager._supervise = True
    # A newer child is already installed by the time this drainer sees EOF.
    manager._process = object()  # type: ignore[assignment]
    before = manager._restart_count

    await manager._drain_logs(proc)
    await proc.wait()

    assert manager._restart_count == before, "stale drainer restarted a live child"
    assert manager._supervisor_task is None


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


class _FakeManager:
    """Enough of the manager for the app lifespan and the probe.

    The lifespan calls ``start()``/``stop()``, so a stub with only
    ``is_ready()`` fails during client setup rather than in the assertion.
    """

    def __init__(self, ready: bool | Exception) -> None:
        self._ready = ready

    async def start(self) -> bool:
        return True

    async def stop(self) -> None:
        return None

    async def is_ready(self) -> bool:
        if isinstance(self._ready, Exception):
            raise self._ready
        return self._ready


def _client(monkeypatch, ready):
    from fastapi.testclient import TestClient

    from mcp_admin_core import app as app_mod

    monkeypatch.setattr(app_mod, "get_process_manager", lambda: _FakeManager(ready))
    return TestClient(app_mod.create_app(title="test"), raise_server_exceptions=False)


def test_healthz_is_green_only_when_the_child_serves(monkeypatch) -> None:
    with _client(monkeypatch, ready=True) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_reports_503_when_the_child_is_down(monkeypatch) -> None:
    """The regression that made the outage invisible."""
    with _client(monkeypatch, ready=False) as client:
        response = client.get("/healthz")
    assert response.status_code == 503, "a dead connector must not report healthy"
    assert response.json()["mcp_child"] == "not_ready"


def test_healthz_survives_a_raising_probe(monkeypatch) -> None:
    """The probe must degrade, not 500.

    A health endpoint that throws is indistinguishable from a crashed process
    to most orchestrators, and it hides the real state behind a stack trace.
    """
    with _client(monkeypatch, RuntimeError("probe exploded")) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
