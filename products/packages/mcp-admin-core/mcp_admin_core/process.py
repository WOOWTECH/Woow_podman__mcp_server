"""MCP server subprocess manager.

Starts, stops, and monitors the MCP server process.  The command,
args, and environment are read from the config store's
``mcp_server`` section.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
from typing import Any

from .config import get_config_store
from .launch_policy import (
    STRIPPED_PARENT_ENV,
    LaunchPolicy,
    coerce_port,
    get_launch_policy,
    is_blocked_env_name,
)

logger = logging.getLogger(__name__)

# 子行程最後幾行輸出（stdout+stderr 合流）保留在記憶體，供 status 顯示 exit 原因
# （#1350：子行程開機因缺設定 exit 時，管理頁要看得到「ODOO_URL not set」之類訊息）。
LAST_OUTPUT_LINES = 20

# 子行程意外結束（不是 stop／restart 造成的）時自動重啟的等待秒數，逐次拉長；
# 連續跑超過 STABLE_SECONDS 才算穩定、退避歸零。設定不完整而一啟動就 exit 的
# 情況會退避到 300 秒一次，不會狂轉。
AUTO_RESTART_DELAYS = (2, 5, 15, 30, 60, 120, 300)
STABLE_SECONDS = 120


def _build_subprocess_env(
    connection: dict,
    mcp_cfg: dict,
    policy: LaunchPolicy | None = None,
    cfg: dict | None = None,
) -> dict[str, str]:
    """Compose child env: OS env + connection（大寫並加 env_prefix）+ mcp_server.env overrides。

    #1530：有 app 政策時只注入政策宣告的鍵（connection_keys／env_keys）＋政策推導的 env，
    env_prefix 也以政策為準；沒有政策時沿用舊行為，但一律擋掉 ``BLOCKED_ENV_NAMES`` 這類
    會改變直譯器／動態連結器行為或關掉上游防護的變數。admin 的 JWT_SECRET 不傳給子行程。
    """
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_PARENT_ENV}
    prefix = policy.env_prefix if policy is not None else mcp_cfg.get("env_prefix", "")
    dropped: list[str] = []
    for key, value in (connection or {}).items():
        name = f"{prefix}{str(key).upper()}"
        if policy is not None and key in policy.non_env_connection_keys:
            continue
        if (policy is not None and key not in policy.connection_keys) or is_blocked_env_name(name):
            if value not in (None, ""):  # chart 預設 seed 的空鍵（gateway_url 等）不必每次警告
                dropped.append(name)
            continue
        # Explicit null clears a saved connection value. Override an inherited
        # credential with empty bytes; omission still inherits, and false/0
        # retain their existing string conversion.
        env[name] = "" if value is None else str(value)
    for key, value in (mcp_cfg.get("env") or {}).items():
        if policy is not None and key not in policy.env_keys:
            dropped.append(key)
            continue
        if is_blocked_env_name(key):
            dropped.append(key)
            continue
        env[key] = str(value)
    if policy is not None and policy.derived_env is not None:
        for key, value in policy.derived_env(cfg or {}).items():
            env[key] = str(value)
    if dropped:
        logger.warning("Ignoring env not allowed for the MCP child process: %s", ", ".join(sorted(dropped)))
    return env


def resolve_launch(mcp_cfg: dict, policy: LaunchPolicy | None = None) -> tuple[str, list[str], int]:
    """回傳實際要執行的 (command, args, port)。

    有 app 政策時 command／args 一律用政策（config.json 的值不再被執行，#1530）；沒有政策時沿用
    config.json。port 都取 ``mcp_server.port``（proxy 的轉發目標，管理 API 不允許修改）。
    """
    port = coerce_port(mcp_cfg.get("port", 8000))
    if policy is not None:
        return policy.command, policy.build_args(port), port
    args = mcp_cfg.get("args", [])
    return mcp_cfg.get("command", ""), list(args) if isinstance(args, list) else [], port


class McpProcessManager:
    """Manage the MCP server as an asyncio subprocess."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._running = False
        self._restart_count = 0
        self._last_output: collections.deque[str] = collections.deque(maxlen=LAST_OUTPUT_LINES)
        # 看門狗：子行程意外結束時自動拉起（容器的 liveness 只看 admin 的 /healthz，
        # 子行程死掉時 pod 仍判定健康，過去會無限期停擺、租戶只看到 502／404）。
        self._intentional_stop = False
        self._started_at: float | None = None
        self._auto_restart_task: asyncio.Task | None = None
        self._auto_restart_count = 0
        self._consecutive_failures = 0
        self._next_auto_restart_delay: float | None = None
        self._next_auto_restart_at: float | None = None  # loop.time() 的預定重啟時刻
        self.auto_restart_delays: tuple[float, ...] = AUTO_RESTART_DELAYS
        self.stable_seconds: float = STABLE_SECONDS
        self._attempted_revision: str | None = None
        self._applied_revision: str | None = None
        self._configuration_error: str | None = None
        self._configuration_task: asyncio.Task | None = None
        self._verifying_configuration = False
        self._active_launch: tuple[str, int] | None = None
        self.configuration_probe_timeout = 30.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        async with get_config_store().transition():
            return await self._start_locked()

    async def _start_locked(self) -> bool:
        """Start the MCP server subprocess.  Returns True if started."""
        if self._running and self._process and self._process.returncode is None:
            logger.info("MCP server already running (pid=%s)", self._process.pid)
            return True

        store = get_config_store()
        cfg = await store.load()
        mcp_cfg = cfg.get("mcp_server", {}) or {}
        policy = get_launch_policy()
        command, args, _port = resolve_launch(mcp_cfg, policy)
        from .configuration import launch_revision
        self._attempted_revision = launch_revision(store, cfg)
        self._applied_revision = None

        if not command:
            logger.warning("No MCP server command configured — skipping start")
            return False

        if policy is not None and (
            mcp_cfg.get("command") not in (None, "", command) or mcp_cfg.get("args") not in (None, args)
        ):
            logger.info(
                "config.json mcp_server.command/args differ from the app default; "
                "launching the app default (%s) — the stored values are not executed (#1530)",
                command,
            )

        # Build connection env from config（含 per-app env_prefix；有政策時只注入白名單鍵）
        connection = cfg.get("connection", {}) or {}
        env = _build_subprocess_env(connection, mcp_cfg, policy, cfg)

        cmd = [command] + args
        logger.info("Starting MCP server: %s", " ".join(cmd))

        # 新一輪啟動：清掉上一輪殘留輸出，讓 status 的 last_output 只反映本輪
        self._last_output.clear()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            # _intentional_stop 要等新子行程換上 self._process 之後才清掉（中間沒有 await）。
            # 上面的 await 會讓出事件迴圈：手動 restart 時舊子行程的 drainer 可能在這時醒來，
            # 若旗標已清、self._process 又還是舊的，它會誤判「意外結束」，偷偷把退避計數加一
            # ——之後第一次真的崩潰就從第二階（5 秒）起跳（商用驗收 DATA-NMCP-004-1）。
            self._process = proc
            self._intentional_stop = False
            self._running = True
            self._started_at = asyncio.get_running_loop().time()
            self._active_launch = (command, _port)
            logger.info("MCP server started (pid=%s)", self._process.pid)

            # Start log drainer in background（同時負責偵測意外結束、排程自動重啟）
            asyncio.create_task(self._drain_logs(self._process))
            self._verifying_configuration = True
            probe_snapshot = {**cfg, "mcp_server": {**mcp_cfg, "port": _port}}
            self._configuration_task = asyncio.create_task(
                self._verify_configuration(proc, probe_snapshot, self._attempted_revision))
            return True

        except FileNotFoundError:
            logger.error("MCP server command not found: %s", command)
            self._running = False
            self._configuration_error = "child_start_failed"
            self._verifying_configuration = False
            return False
        except Exception as exc:
            logger.error("Failed to start MCP server (%s)", type(exc).__name__)
            self._running = False
            self._configuration_error = "child_start_failed"
            self._verifying_configuration = False
            return False

    async def stop(self) -> None:
        async with get_config_store().transition():
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        """Gracefully stop the MCP server."""
        # 主動停止：不要讓看門狗把它拉回來，也取消已排程的自動重啟
        self._intentional_stop = True
        self._cancel_auto_restart()
        self._cancel_configuration_probe()
        self._applied_revision = None
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
        """Stop then start the MCP server.  Returns True if restarted."""
        async with get_config_store().transition():
            await self.stop()
            self._restart_count += 1
            return await self.start()

    async def start_or_restart(self) -> bool:
        """套用新設定：運行中 → restart；未運行（含開機因缺設定已 exit）→ start。

        #1350：存連線設定時若只在 ``is_running`` 才 restart，開機 exit 的子行程永遠
        不會被拉起來（租戶只看到 502）。回傳是否成功 spawn。
        """
        async with get_config_store().transition():
            if self.is_running:
                return await self.restart()
            return await self.start()

    def _cancel_configuration_probe(self) -> None:
        task = self._configuration_task
        if task is not None and not task.done():
            task.cancel()
        self._configuration_task = None
        self._verifying_configuration = False

    async def _verify_configuration(self, proc, snapshot, revision) -> None:
        """Read-only MCP readiness, bound to the exact process and launch snapshot.

        A successful spawn is pending until initialize/list works. This says
        nothing about backend credentials or a tool's ability to perform work.
        """
        from .capabilities import discover_tools
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.configuration_probe_timeout
        try:
            while proc is self._process and proc.returncode is None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    async with asyncio.timeout(remaining):
                        await discover_tools(snapshot)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(min(0.25, max(0, deadline - loop.time())))
                    continue
                if proc is self._process and proc.returncode is None and self._attempted_revision == revision:
                    self._applied_revision = revision
                    self._configuration_error = None
                return
            if proc is self._process and proc.returncode is None:
                self._configuration_error = "child_probe_failed"
        finally:
            if self._configuration_task is asyncio.current_task():
                self._verifying_configuration = False
                self._configuration_task = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        """Return current process status."""
        store = get_config_store()
        cfg = await store.load()
        mcp_cfg = cfg.get("mcp_server", {}) or {}
        command, _args, port = resolve_launch(mcp_cfg, get_launch_policy())
        from .configuration import configuration_status
        configuration = configuration_status(store, cfg, self)

        if self._process and self._process.returncode is None:
            return {
                "running": True,
                "pid": self._process.pid,
                "restart_count": self._restart_count,
                "auto_restart_count": self._auto_restart_count,
                "auto_restart_pending": False,
                "next_auto_restart_in": None,
                "command": self._active_launch[0] if self._active_launch else command,
                "port": self._active_launch[1] if self._active_launch else port,
                "configuration": configuration,
            }
        return {
            "running": False,
            "pid": None,
            "restart_count": self._restart_count,
            "auto_restart_count": self._auto_restart_count,
            "auto_restart_pending": self._auto_restart_pending,
            "next_auto_restart_in": self._next_auto_restart_in(),
            "command": command,
            "port": port,
            "exit_code": self._process.returncode if self._process else None,
            "last_output": list(self._last_output),
            "configuration": configuration,
        }

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def child_pid(self) -> int | None:
        """執行中子行程的 PID；沒有在跑時為 None。閘道用它分辨子行程換過沒有（``session_reclaim``）。"""
        return self._process.pid if self.is_running else None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @property
    def _auto_restart_pending(self) -> bool:
        return self._auto_restart_task is not None and not self._auto_restart_task.done()

    def _next_auto_restart_in(self) -> float | None:
        """距離下次自動重啟還有幾秒（倒數，取到 0.1 秒）；沒有排程時為 None。"""
        if not self._auto_restart_pending or self._next_auto_restart_at is None:
            return None
        remaining = self._next_auto_restart_at - asyncio.get_running_loop().time()
        return round(max(0.0, remaining), 1)

    def _cancel_auto_restart(self) -> None:
        task = self._auto_restart_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._auto_restart_task = None
        self._next_auto_restart_delay = None
        self._next_auto_restart_at = None

    async def _drain_logs(self, proc: asyncio.subprocess.Process | None = None) -> None:
        """Read stdout/stderr from the subprocess and log it；結束後判斷要不要自動重啟。"""
        proc = proc or self._process
        if not proc or not proc.stdout:
            return
        try:
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._last_output.append(text)
                    logger.info("[mcp-server] %s", text)
        except Exception:
            pass

        # Process exited
        try:
            rc = await proc.wait()
        except Exception:
            rc = proc.returncode
        if proc is not self._process:
            # 已被 restart 換成新的子行程：舊的結束是預期中的
            return
        self._running = False
        self._applied_revision = None
        self._cancel_configuration_probe()
        if self._intentional_stop:
            logger.info("MCP server exited with code %s (stopped)", rc)
            return
        logger.warning("MCP server exited unexpectedly with code %s", rc)
        self._configuration_error = "child_exited"
        self._schedule_auto_restart()

    def _schedule_auto_restart(self) -> None:
        if self._auto_restart_pending:
            return
        loop = asyncio.get_running_loop()
        if self._started_at is not None and loop.time() - self._started_at >= self.stable_seconds:
            self._consecutive_failures = 0
        delays = self.auto_restart_delays
        delay = delays[min(self._consecutive_failures, len(delays) - 1)]
        self._consecutive_failures += 1
        self._next_auto_restart_delay = delay
        self._next_auto_restart_at = loop.time() + delay
        logger.warning("MCP server auto-restart scheduled in %ss (attempt %d)", delay, self._consecutive_failures)
        self._auto_restart_task = asyncio.create_task(self._auto_restart_after(delay))

    async def _auto_restart_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with get_config_store().transition():
            if self.is_running or self._intentional_stop:
                return
            self._auto_restart_count += 1
            self._next_auto_restart_delay = None
            self._next_auto_restart_at = None
            logger.warning("MCP server auto-restarting (auto_restart_count=%d)", self._auto_restart_count)
            if not await self.start():
                logger.error("MCP server auto-restart failed to spawn; giving up until next manual start")


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
