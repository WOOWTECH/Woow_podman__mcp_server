"""MCP server subprocess manager.

Starts, stops, and monitors the MCP server process.  The command,
args, and environment are read from the config store's
``mcp_server`` section.

The child environment is built as: ``os.environ`` + the UPPER-CASED
``connection`` section (so ``podman_uri`` becomes
``PODMAN_URI`` and ``podman_api_version`` becomes
``PODMAN_API_VERSION``, matching the FastMCP server's Settings
``env_prefix``) + explicit ``mcp_server.env`` overrides (where the
ToolManager writes the tool/permission switches, e.g.
``PODMAN_MCP_DISABLED_TOOLS``, ``PODMAN_MCP_PROFILE``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

from .config import get_config_store

logger = logging.getLogger(__name__)

# The FastMCP child needs 10-14s to import and bind in production, so anything
# shorter turns a healthy start into a "not ready" report.
_READY_TIMEOUT = 30.0
_READY_POLL_INTERVAL = 0.5
_READY_CONNECT_TIMEOUT = 1.0

# Supervision. A child that dies used to stay dead: the log drainer noticed the
# exit, wrote one WARNING, and returned. The console kept serving its login page
# with /healthz reporting 200 while every connector request answered
# "Connection refused" — an outage nothing recovered from and nothing reported.
#
# Backoff is capped rather than bounded by an attempt limit: giving up converts
# a transient crash (OOM kill, a bad config the operator is about to correct)
# into a permanent outage, which is precisely the failure this exists to end.
# The cap keeps a child that crashes instantly from becoming a spin loop.
_RESTART_BACKOFF_START = 1.0
_RESTART_BACKOFF_MAX = 60.0
# Stay up this long and the crash counts as "recovered", so an unrelated crash
# next week starts from 1s again instead of inheriting an hour-old backoff.
_RESTART_BACKOFF_RESET_AFTER = 120.0


def _inject_port(args: list[str], port: int | None) -> list[str]:
    """Make ``mcp_server.port`` authoritative for the child's bind port.

    The command line used to be passed through verbatim while the reverse proxy
    dialled ``mcp_server.port``, so the GUI's Port field was decorative: change
    it and the proxy started calling a port nothing was listening on.  Here the
    stored port overrides an existing ``--port`` and is appended when the child
    is clearly an HTTP server but was given no port at all.  A stdio child gets
    nothing — it has no port to bind.
    """
    if not port:
        return list(args)

    argv = list(args)
    if "stdio" in argv:  # --transport stdio: no listener, nothing to inject
        return argv

    for index, arg in enumerate(argv):
        if arg == "--port":
            if index + 1 < len(argv):
                argv[index + 1] = str(port)
                return argv
            return argv + [str(port)]
        if arg.startswith("--port="):
            argv[index] = f"--port={port}"
            return argv

    # Only append for a command line that already looks like an HTTP server;
    # an unknown child would reject an argument it does not define.
    if any(a in ("--host", "--transport") or a.startswith(("--host=", "--transport=")) for a in argv):
        return argv + ["--port", str(port)]
    return argv


async def _port_accepts_connections(port: int) -> bool:
    """True if something is listening on 127.0.0.1:*port*."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=_READY_CONNECT_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError):
        pass
    return True


class McpProcessManager:
    """Manage the MCP server as an asyncio subprocess."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._running = False
        self._restart_count = 0
        # Port the *current* child was told to bind, and whether it binds one
        # at all (a stdio child never does).  Set at start() from the config.
        self._port: int | None = None
        self._expects_port = False
        # Supervision state.  ``_supervise`` is flipped off by stop() so an
        # operator-requested shutdown is not immediately undone by the watchdog,
        # and back on by start().
        self._supervise = False
        self._backoff = _RESTART_BACKOFF_START
        self._supervisor_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Start the MCP server subprocess.  Returns True if started."""
        if self._running and self._process and self._process.returncode is None:
            logger.info("MCP server already running (pid=%s)", self._process.pid)
            return True

        store = get_config_store()
        mcp_cfg = await store.get("mcp_server", {})
        command = mcp_cfg.get("command", "")

        if not command:
            logger.warning("No MCP server command configured — skipping start")
            return False

        args = list(mcp_cfg.get("args", []) or [])
        env_overrides = mcp_cfg.get("env", {})

        try:
            port = int(mcp_cfg.get("port") or 0) or None
        except (TypeError, ValueError):
            port = None
        args = _inject_port(args, port)
        # The proxy dials this port; remember what we actually told the child
        # to bind so status()/wait_ready() probe the same address.
        self._expects_port = bool(port) and "stdio" not in args
        self._port = port if self._expects_port else None

        # Build connection env from config
        connection = await store.get("connection", {})

        # Merge: OS env + connection config + mcp_server.env overrides
        env = {**os.environ}
        for key, value in connection.items():
            env[key.upper()] = str(value)
        for key, value in env_overrides.items():
            env[key] = str(value)

        cmd = [command] + args
        logger.info("Starting MCP server: %s", " ".join(cmd))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._running = True
            self._supervise = True
            logger.info("MCP server started (pid=%s)", self._process.pid)

            # Start log drainer in background.  The child is passed in rather
            # than read from self later: by the time the drainer sees EOF,
            # self._process may already point at a *newer* child (an operator
            # restart racing a crash), and supervising on the stale reference
            # would restart a process that is running fine.
            asyncio.create_task(self._drain_logs(self._process))
            return True

        except FileNotFoundError:
            logger.error("MCP server command not found: %s", command)
            self._running = False
            return False
        except Exception as exc:
            logger.error("Failed to start MCP server: %s", exc)
            self._running = False
            return False

    async def stop(self) -> None:
        """Gracefully stop the MCP server."""
        # Disarm supervision FIRST, before any signal is sent: otherwise the
        # drainer sees the exit we are about to cause and helpfully restarts
        # the process the operator just asked to stop.
        self._supervise = False
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            self._supervisor_task = None

        if not self._process or self._process.returncode is not None:
            self._running = False
            return

        pid = self._process.pid
        logger.info("Stopping MCP server (pid=%s)", pid)

        try:
            self._process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("MCP server did not stop in 10s, killing")
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            pass

        self._running = False
        logger.info("MCP server stopped")

    async def restart(self) -> bool:
        """Stop then start the MCP server, waiting until it accepts connections.

        Returns True only when the child is actually serving.  A bare "process
        started" answer used to come back in 0.3s while the connector kept
        returning 502 for another 10-14s, so every save reported success on an
        outage it had just caused.
        """
        await self.stop()
        self._restart_count += 1
        if not await self.start():
            return False
        return await self.wait_ready()

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------

    async def is_ready(self) -> bool:
        """True when the child is up *and* accepting connections on its port."""
        if not self.is_running:
            return False
        if not self._expects_port or not self._port:
            # A stdio child has no listener; liveness is all we can observe.
            return True
        return await _port_accepts_connections(self._port)

    async def wait_ready(self, timeout: float = _READY_TIMEOUT) -> bool:
        """Poll until the child serves, it dies, or *timeout* elapses."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if not self.is_running:
                return False
            if await self.is_ready():
                return True
            if loop.time() >= deadline:
                logger.warning(
                    "MCP server still not accepting connections on port %s after %.0fs",
                    self._port,
                    timeout,
                )
                return False
            await asyncio.sleep(_READY_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        """Return current process status.

        ``running`` is process liveness, ``ready`` is "serving".  They differ
        while the child is starting up and for a child that is alive but
        wedged — reporting only ``running`` made the GUI claim health during
        the restart window in which the connector answered 502.
        """
        store = get_config_store()
        mcp_cfg = await store.get("mcp_server", {})
        port = self._port or mcp_cfg.get("port", 8000)

        if self._process and self._process.returncode is None:
            ready = await self.is_ready()
            return {
                "running": True,
                "ready": ready,
                "state": "running" if ready else "starting",
                "pid": self._process.pid,
                "restart_count": self._restart_count,
                "command": mcp_cfg.get("command", ""),
                "port": port,
            }
        return {
            "running": False,
            "ready": False,
            "state": "exited" if self._process else "stopped",
            "pid": None,
            "restart_count": self._restart_count,
            "command": mcp_cfg.get("command", ""),
            "port": port,
            "exit_code": self._process.returncode if self._process else None,
        }

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _drain_logs(self, proc: asyncio.subprocess.Process) -> None:
        """Read stdout/stderr from *proc* and log it, then supervise its exit.

        The log lines are emitted through the standard logger so the
        ``logs`` router can tap them and stream them to the GUI over SSE.
        """
        if not proc.stdout:
            return
        started_at = asyncio.get_running_loop().time()
        try:
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.info("[mcp-server] %s", text)
        except Exception:
            pass

        # Process exited.
        rc = proc.returncode
        uptime = asyncio.get_running_loop().time() - started_at

        # Only speak for the child we were started for.  If self._process has
        # moved on, a newer child is live and this exit is already handled.
        if self._process is not proc:
            logger.info("MCP server (superseded child) exited with code %s", rc)
            return

        self._running = False
        logger.warning("MCP server exited with code %s after %.1fs", rc, uptime)

        if not self._supervise:
            logger.info("Supervision disarmed — not restarting")
            return

        if uptime >= _RESTART_BACKOFF_RESET_AFTER:
            self._backoff = _RESTART_BACKOFF_START

        self._supervisor_task = asyncio.create_task(self._resurrect(proc))

    async def _resurrect(self, dead: asyncio.subprocess.Process) -> None:
        """Bring the child back after an unrequested exit, with backoff."""
        try:
            while self._supervise and self._process is dead:
                delay = self._backoff
                logger.warning("Restarting MCP server in %.0fs", delay)
                await asyncio.sleep(delay)
                self._backoff = min(self._backoff * 2, _RESTART_BACKOFF_MAX)

                if not self._supervise or self._process is not dead:
                    return  # an operator got there first

                self._restart_count += 1
                if not await self.start():
                    continue  # start() failed outright; back off and retry
                if await self.wait_ready():
                    logger.info("MCP server recovered after unrequested exit")
                    return
                # Started but never served.  start() replaced self._process, so
                # that child's own drainer now owns the next round; stepping out
                # here keeps exactly one supervisor alive per child.
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Supervisor failed to restart MCP server: %s", exc)


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_instance: McpProcessManager | None = None


def get_process_manager() -> McpProcessManager:
    """Return the global McpProcessManager singleton."""
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = McpProcessManager()
    return _instance
