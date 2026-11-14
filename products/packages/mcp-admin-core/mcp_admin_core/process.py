"""MCP server subprocess manager.

Starts, stops, and monitors the MCP server process.  The command,
args, and environment are read from the config store's
``mcp_server`` section.

子行程身分與生命週期（blocker 3，``docs/security/mcp-child-identity.md``）：每個子行程在自己的 session／
process group 啟動，Linux 上管理程序死掉時子行程收到 SIGKILL（``child_identity.spawn_argv``）；停止時對整個
group 送訊號，leader 結束後確認 group 沒有成員留下；spawn 前拒絕已被佔用、或與其他 profile／管理程序
listener 相同的 port；只有 OS 層確認「會接受 127.0.0.1:P 連線的每一個 LISTEN socket 都屬於這個子行程的
session」之後，這個世代才可以接收 MCP 流量（:meth:`McpProcessManager.forward_generation`）。
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import logging
import os
import signal
from dataclasses import dataclass
from typing import Any

from . import child_identity
from .child_identity import Peer
from .config import get_config_store
from .log_redaction import PathTokenRedactionFilter
from .owner_defaults import _StdoutHandler
from .launch_policy import (
    STRIPPED_PARENT_ENV,
    LaunchPolicy,
    coerce_port,
    get_launch_policy,
    is_blocked_env_name,
)

logger = logging.getLogger(__name__)


#: ``_drain_logs`` 在子行程輸出的 log record 上帶的結構標記（``extra``）。stdout 排除只看這個屬性，不看訊息文字：
#: 遮罩會把整行換成「[redacted: ...]」，前綴「[mcp-server] 」就不見了。
CHILD_OUTPUT_ATTR = "mcp_child_output"

#: 子行程輸出的一行最多留這麼多位元組（不含換行）；更長的部分丟掉、行尾標上 ``…[truncated N bytes]``。與
#: ``asyncio.StreamReader`` 的預設 limit 相同：以前整行收得下的（≤ 64 KiB）一個位元組都不動，更長的行才會被截斷
#: （以前那種行讓 ``_drain_logs`` 因 ``ValueError`` 離開迴圈、不再讀 pipe）。
MAX_CHILD_LINE_BYTES = 64 * 1024
_CHILD_READ_BYTES = 64 * 1024


class _ChildOutputExcluded(logging.Filter):
    """子行程自己的 stdout／stderr（``_drain_logs`` 帶 :data:`CHILD_OUTPUT_ATTR` 的 record）只進管理頁 Logs，不進容器 stdout（可能含後端錯誤內容）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, CHILD_OUTPUT_ATTR, False) is not True


def _install_stdout_lifecycle_log() -> None:
    """子行程生命週期／身分拒絕（child_port_in_use、child_peer_mismatch、auto-restart…）也寫到容器 stdout，``kubectl logs`` 才看得到。

    與 owner_defaults／origin_security 相同：admin 沒設 root logger，所以 logger 自己掛 :class:`_StdoutHandler`，等級 INFO；
    紀錄仍往上傳給 ring buffer（不重複）。handler 上再掛一道遮罩 filter，不依賴 record factory 是否已安裝。
    """
    if any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
        return
    handler = _StdoutHandler()
    handler.setLevel(logging.INFO)
    handler.addFilter(_ChildOutputExcluded())
    handler.addFilter(PathTokenRedactionFilter())
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)  # root 預設 WARNING，INFO 的啟動／停止紀錄也要印


_install_stdout_lifecycle_log()

# 子行程最後幾行輸出（stdout+stderr 合流）保留在記憶體，供 status 顯示 exit 原因
# （#1350：子行程開機因缺設定 exit 時，管理頁要看得到「ODOO_URL not set」之類訊息）。
LAST_OUTPUT_LINES = 20

# 子行程意外結束（不是 stop／restart 造成的）時自動重啟的等待秒數，逐次拉長；
# 連續跑超過 STABLE_SECONDS 才算穩定、退避歸零。設定不完整而一啟動就 exit 的
# 情況會退避到 300 秒一次，不會狂轉。
AUTO_RESTART_DELAYS = (2, 5, 15, 30, 60, 120, 300)
STABLE_SECONDS = 120
INITIALIZE_DRAIN_TIMEOUT = 0.25
#: 持設定鎖的生命週期步驟（存連線觸發的 restart、工具政策的 restart、看門狗的 start）讓已取消的 initialize
#: 派送跑完 finally 的上限。超過不在鎖內繼續等：步驟失敗、舊世代維持隔離，改由 :meth:`_defer_until_drained`
#: 在派送結束後自動收斂。
INITIALIZE_QUIESCE_TIMEOUT = 0.5
STOP_TIMEOUT = 10
#: 停止時 leader 已結束、同一個 process group 其他成員（也收到了 SIGTERM）自己結束的寬限；之後 SIGKILL。
GROUP_GRACE_SECONDS = 2.0
#: SIGKILL 之後等成員消失的上限；超過只記錯誤（下一次 spawn 前的 port 檢查仍會擋住）。
GROUP_KILL_WAIT_SECONDS = 2.0
#: 子行程開始聽 port 之前，確認身分的輪詢間隔。
PEER_POLL_SECONDS = 0.1
#: 探測逾時之後仍存活的子行程，背景身分檢查的間隔上限（由 PEER_POLL_SECONDS 起逐步退避到這個值）。
LATE_VERIFY_MAX_DELAY = 5.0
#: 轉發連不上時，請求最多等多久重新確認 listener（確認本身在背景跑完）。
PEER_RECHECK_WAIT = 1.0
#: 身分不符、要停掉的世代：SIGTERM 之後多久 SIGKILL。
REFUSED_STOP_TIMEOUT = 5

#: ``configuration.last_error_code``：spawn 前 port 已被其他 socket 佔用（含管理程序自己正在聽的 port）。
CHILD_PORT_IN_USE = "child_port_in_use"
#: spawn 前 port 與其他 profile（指派或正在跑的子行程）、或管理程序命令列的 port 衝突。
CHILD_PORT_CONFLICT = "child_port_conflict"
#: 子行程啟動期間，會接受 127.0.0.1:P 連線的 listener 不屬於（或看不出屬於）子行程的 session。
CHILD_PEER_MISMATCH = "child_peer_mismatch"
#: 已驗證的世代在轉發連不上之後重新檢查：子行程不再擁有 listener。
CHILD_PEER_LOST = "child_peer_lost"


async def _child_output_lines(stream):
    """子行程輸出的每一行，``(內容, 丟掉的位元組數)``：每行最多留 :data:`MAX_CHILD_LINE_BYTES`，記憶體有上限。

    不用 ``readline()``：``StreamReader`` 遇到超過 limit 的行會拋 ``ValueError``，而且已經把那一段丟掉，連前面能留的部分
    都拿不到；呼叫端一旦放棄迴圈就沒有人讀 pipe 了。這裡直接讀區塊自己切行，超過上限的部分只計數、不存。被丟掉的部分
    **不會另外成行**：token 若被切在兩行，第二行沒有 ``/private_`` 前綴就遮不到。最後沒有換行的一行（EOF）照樣交出。

    只能逐行迭代的輸出（測試替身，沒有 ``read``）照舊一行一行讀，超過上限的行同樣截斷。
    """
    read = getattr(stream, "read", None)
    if not callable(read):
        async for line in stream:
            body = line[:-1] if line.endswith(b"\n") else line
            yield body[:MAX_CHILD_LINE_BYTES], max(0, len(body) - MAX_CHILD_LINE_BYTES)
        return
    pending = bytearray()
    dropped = 0
    while True:
        chunk = await read(_CHILD_READ_BYTES)
        if not chunk:
            break
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            piece = chunk[start:] if end < 0 else chunk[start:end]
            room = MAX_CHILD_LINE_BYTES - len(pending)
            if len(piece) > room:
                pending += piece[:room]
                dropped += len(piece) - room
            else:
                pending += piece
            if end < 0:
                break
            yield bytes(pending), dropped
            pending.clear()
            dropped = 0
            start = end + 1
    if pending or dropped:
        yield bytes(pending), dropped


class InitializeDrainPending(RuntimeError):
    """A cancelled initialize dispatch of the old generation is still registered; nothing was stopped."""


@dataclass
class _ChildGroup:
    """The process group this manager created at spawn time (``start_new_session``: PGID＝SID＝leader PID)."""
    proc: Any
    pgid: int
    port: int
    stopping: bool = False
    sweeper: asyncio.Task | None = None


@dataclass
class _Deferred:
    """A lifecycle step that could not run while an old initialize dispatch was registered."""
    proc: Any
    changed: asyncio.Event
    action: str  # "stop" | "start" | "restart"
    task: asyncio.Task | None = None


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
        # Configuration keys themselves are untrusted; they may contain a
        # credential accidentally or deliberately placed in a field name.
        logger.warning("Ignoring %d env field(s) not allowed for the MCP child process", len(dropped))
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


async def _leader_exit(proc, timeout: float | None) -> bool:
    """Wait until the leader itself was reaped (``returncode`` set); False when *timeout* passes first.

    ``asyncio`` ``Process.wait()`` also waits until every inherited stdout/stderr pipe is closed, so a descendant
    that keeps the pipe would block it, and the group sweep is what removes such a descendant. Linux: wake up on
    the leader's pidfd (readable at exit); asyncio sets ``returncode`` within a loop turn after reaping it.
    """
    if proc.returncode is not None:
        return True
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    exited = pidfd = None
    if hasattr(os, "pidfd_open") and isinstance(proc, asyncio.subprocess.Process):
        try:
            pidfd = os.pidfd_open(proc.pid)  # no await since returncode was None: the PID is still this child
        except OSError:
            pidfd = None
    try:
        if pidfd is not None:
            exited = loop.create_future()
            loop.add_reader(pidfd, lambda: exited.done() or exited.set_result(None))
        while proc.returncode is None:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            if exited is not None and not exited.done():
                try:
                    await asyncio.wait_for(asyncio.shield(exited), 5.0 if remaining is None else min(5.0, remaining))
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0.02 if exited is not None else 0.25)
        return True
    finally:
        if pidfd is not None:
            loop.remove_reader(pidfd)
            os.close(pidfd)


class McpProcessManager:
    """Manage the MCP server as an asyncio subprocess."""

    def __init__(self, profile_id: str = "default") -> None:
        #: The profile this child belongs to (``profiles.DEFAULT_PROFILE`` or a 24-hex id, never a label): lifecycle
        #: log lines carry it with the port and PID so ``kubectl logs`` tells which profile's child a line is about.
        self.profile_id = profile_id
        #: Port of the latest start attempt (the refused one included); the subject of watchdog log lines.
        self._launch_port: int | None = None
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
        #: 探測逾時後、子行程仍活著時的背景身分檢查（同一套驗證，低頻退避）；停止、換代、子行程結束時取消。
        self._late_verify_task: asyncio.Task | None = None
        self.late_verify_max_delay = LATE_VERIFY_MAX_DELAY
        self._verifying_configuration = False
        self._active_launch: tuple[str, int] | None = None
        self.configuration_probe_timeout = 30.0
        self._initialize_generation_changed = asyncio.Event()
        self._initialize_dispatches: set[asyncio.Task] = set()
        self._initialize_waiters = 0
        self._lifecycle_reservation = None
        self._lifecycle_intent = "stop"
        # 子行程身分（blocker 3）
        self._group: _ChildGroup | None = None
        #: (世代事件, port)：這個世代的 listener 通過 OS 身分檢查；世代失效時清掉。
        self._peer_verified: tuple[asyncio.Event, int] | None = None
        #: (process, 錯誤碼)：管理程序因身分問題停掉這個子行程；它結束時保留這個錯誤碼、不改成 child_exited。
        self._generation_fault: tuple[Any, str] | None = None
        self._peer_recheck: asyncio.Task | None = None
        self._peer_unsupported_logged = False
        self._deferred: _Deferred | None = None

    def _tag(self, proc=None, port: int | None = None) -> str:
        """``[profile=<id> port=<port> pid=<pid>]`` for a lifecycle log line (unknown parts omitted; never a token).

        *proc* defaults to the current child while it runs (an exited one is named only when passed explicitly) and
        *port* to the latest start attempt's port.
        """
        if proc is None and self.is_running:
            proc = self._process
        port = self._launch_port if port is None else port
        pid = getattr(proc, "pid", None)
        parts = [f"profile={self.profile_id}"]
        if port is not None:
            parts.append(f"port={port}")
        if pid is not None:
            parts.append(f"pid={pid}")
        return "[" + " ".join(parts) + "]"

    def initialize_generation(self):
        """Capture the managed process instance and its invalidation event."""
        return self._process, self._initialize_generation_changed

    def begin_initialize_dispatch(self, generation):
        """Reserve the process synchronously, before any dispatch await."""
        proc, changed = generation
        if (changed.is_set() or proc is None or proc is not self._process
                or proc.returncode is not None or changed is not self._initialize_generation_changed):
            return False
        self._initialize_dispatches.add(asyncio.current_task())
        return True

    def end_initialize_dispatch(self):
        self._initialize_dispatches.discard(asyncio.current_task())

    def forward_generation(self, port: int):
        """``(process, generation event)`` MCP traffic for ``127.0.0.1:port`` may be forwarded to, else None.

        Only the current, running, not invalidated generation whose listener passed the OS identity check for
        exactly this port (without /proc: whose probe succeeded). The proxy checks this immediately before each
        upstream send; anything else stays on the not-running/setup/503 path and is never forwarded.
        """
        peer = self._peer_verified
        proc, changed = self._process, self._initialize_generation_changed
        if (peer is None or peer[0] is not changed or changed.is_set() or peer[1] != port
                or proc is None or proc.returncode is not None):
            return None
        return proc, changed

    def _invalidate_initialize_generation(self):
        self._initialize_generation_changed.set()
        self._peer_verified = None
        for task in tuple(self._initialize_dispatches):
            task.cancel()

    async def _quiesce_initialize_dispatches(self):
        """Give cancelled dispatches a bounded chance to finish; never an unbounded wait under a config lock.

        Cancellation is delivered at the dispatch's next await, so a cooperative dispatch needs one scheduling
        round to run its ``finally``. One that is still registered after ``INITIALIZE_QUIESCE_TIMEOUT`` keeps
        the old generation fenced; the caller defers the lifecycle step (:meth:`_defer_until_drained`).
        """
        pending = tuple(self._initialize_dispatches)
        if pending:
            await asyncio.wait(pending, timeout=INITIALIZE_QUIESCE_TIMEOUT)
        if self._initialize_dispatches:
            raise InitializeDrainPending("MCP initialize dispatch did not stop")

    def prepare_initialize_drain(self, intent: str = "stop"):
        """Reserve one lifecycle operation and invalidate under transition lock."""
        if self._lifecycle_reservation is not None:
            raise RuntimeError("MCP lifecycle operation already pending")
        ticket = (object(), self._process, self._initialize_generation_changed)
        self._lifecycle_reservation = ticket
        self._lifecycle_intent = intent
        self._cancel_deferred()
        self._intentional_stop = True
        self._cancel_auto_restart()
        self._cancel_configuration_probe()
        self._applied_revision = None
        self._invalidate_initialize_generation()
        return ticket

    async def drain_initializes(self, ticket):
        """Give cancellations a bounded chance to finish outside the lock."""
        if self._lifecycle_reservation is not ticket:
            raise RuntimeError("MCP lifecycle reservation changed")
        pending = tuple(self._initialize_dispatches)
        if pending:
            await asyncio.wait(pending, timeout=INITIALIZE_DRAIN_TIMEOUT)
        if self._initialize_dispatches:
            # This operation reports failure (its outcome stays unknown); the manager converges on its own
            # once the old generation's dispatches are gone, never earlier.
            self._defer_until_drained(self._lifecycle_intent, ticket[1], ticket[2])
            raise InitializeDrainPending("MCP initialize dispatch did not stop")

    def validate_initialize_drain(self, ticket):
        if (self._lifecycle_reservation is not ticket or self._process is not ticket[1]
                or self._initialize_generation_changed is not ticket[2]
                or not ticket[2].is_set() or self._initialize_dispatches):
            raise RuntimeError("MCP lifecycle generation changed")

    def release_initialize_drain(self, ticket):
        if self._lifecycle_reservation is ticket:
            self._lifecycle_reservation = None

    # ------------------------------------------------------------------
    # Deferred convergence (initialize dispatch still in flight)
    # ------------------------------------------------------------------

    def _defer_until_drained(self, action: str, proc=None, changed=None) -> None:
        """Run *action* once the fenced generation's initialize dispatches have ended.

        The old generation stays invalidated meanwhile (no new initialize, no forward to it) and no replacement
        child starts before the last old dispatch is gone; the step re-checks, under the transition lock, that
        nothing else moved the child on. ``start``/``restart`` win over ``stop`` for the same generation.
        """
        proc = self._process if proc is None else proc
        changed = self._initialize_generation_changed if changed is None else changed
        current = self._deferred
        if (current is not None and current.task is not None and not current.task.done()
                and current.changed is changed and current.proc is proc):
            if action != "stop":
                current.action = action
            return
        self._cancel_deferred()
        deferred = _Deferred(proc, changed, action)
        self._deferred = deferred
        deferred.task = asyncio.create_task(self._apply_after_drain(deferred))
        logger.warning("MCP %s deferred: an initialize dispatch of the fenced generation is still in flight; "
                       "no replacement child starts before it ends %s", action, self._tag())

    def _cancel_deferred(self) -> None:
        deferred, self._deferred = self._deferred, None
        task = deferred.task if deferred is not None else None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _apply_after_drain(self, deferred: _Deferred) -> None:
        def superseded():
            return (self._deferred is not deferred or self._process is not deferred.proc
                    or self._initialize_generation_changed is not deferred.changed)
        try:
            while not superseded():
                pending = tuple(self._initialize_dispatches)
                if not pending:
                    break
                await asyncio.wait(pending)
            async with get_config_store().transition():
                if (superseded() or not deferred.changed.is_set() or self._initialize_dispatches
                        or self._lifecycle_reservation is not None):
                    return
                self._deferred = None
                logger.warning("MCP initialize dispatches of the fenced generation ended; running the deferred %s %s",
                               deferred.action, self._tag())
                await self._stop_locked()
                if deferred.action != "stop":
                    if deferred.action == "restart":
                        self._restart_count += 1
                    await self._start_locked()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Deferred MCP %s failed (%s) %s", deferred.action, type(exc).__name__, self._tag())
        finally:
            if self._deferred is deferred:
                self._deferred = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        from .profiles import DEFAULT_PROFILE, current_profile_id, get_profile_registry
        profile_id = current_profile_id()
        if profile_id != DEFAULT_PROFILE:
            refusal = await get_profile_registry().capacity_refusal(profile_id)
            if refusal is not None:
                # The profile stays listed (GET /api/profiles disabled_reason); its URL gets the not-running answer.
                logger.warning("MCP child is not started: %s %s", refusal, self._tag())
                return False
        async with get_config_store().transition():
            try:
                return await self._start_locked()
            except InitializeDrainPending:
                self._defer_until_drained("start")
                raise

    async def _start_locked(self) -> bool:
        """Start the MCP server subprocess.  Returns True if started."""
        if self._running and self._process and self._process.returncode is None:
            if not self._initialize_generation_changed.is_set():
                logger.info("MCP server already running (pid=%s)", self._process.pid)
                return True
            if self._lifecycle_reservation is not None:
                logger.warning("MCP child generation is invalidated; stop it before starting again")
                return False
            # A fenced generation (a stop that could not finish) is replaced; while one of its initialize
            # dispatches is still registered this raises InitializeDrainPending and the caller defers.
            logger.warning("MCP child generation is fenced; replacing it")
            await self._stop_locked()

        # An exited generation must not share its loopback address with a new
        # managed child while a cancelled dispatch still owns that generation.
        self._invalidate_initialize_generation()
        await self._quiesce_initialize_dispatches()
        self._cancel_configuration_probe()  # no background check of an earlier generation outlives it

        store = get_config_store()
        cfg = await store.load()
        mcp_cfg = cfg.get("mcp_server", {}) or {}
        policy = get_launch_policy()
        command, args, _port = resolve_launch(mcp_cfg, policy)
        self._launch_port = _port
        from .configuration import launch_revision
        self._attempted_revision = launch_revision(store, cfg)
        self._applied_revision = None

        if not command:
            logger.warning("No MCP server command configured — skipping start")
            return False

        # A product child that exits without these fields (LaunchPolicy.required_connection) is not spawned until they
        # are saved; saving the connection starts it (start_or_restart). Every profile, the default included, so no
        # exit-and-auto-restart loop before the first save (2026-11-13 mp-products: the Headscale default profile was
        # restarted 2, 5, 15… s apart until configured). configuration_status reports "unconfigured".
        missing = policy.missing_connection(cfg.get("connection")) if policy is not None else []
        if missing:
            self._configuration_error = None
            self._running = False
            self._verifying_configuration = False
            self._started_at = None
            logger.info("MCP child is not started: required connection field(s) %s are empty; it starts when they are "
                        "saved on the Connection page %s", ", ".join(key for key, _label in missing), self._tag())
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
        # Child launchers that read a config file (Headscale, Nextcloud) must
        # see this profile's file. This trusted assignment comes after the
        # allowlisted env builder; management settings cannot override it.
        env["MCP_ADMIN_CONFIG"] = str(store.path)

        refusal = await self._port_refusal(_port)
        if refusal is not None:
            code, reason = refusal
            logger.error("Refusing to start the MCP child on 127.0.0.1:%s (%s): %s; no MCP request is forwarded "
                         "to that port %s", _port, code, reason, self._tag(port=_port))
            self._configuration_error = code
            self._running = False
            self._verifying_configuration = False
            self._started_at = None
            # The holder may be a stale process that is still exiting: keep retrying with the watchdog's backoff
            # until an explicit stop (each attempt repeats every check).
            self._intentional_stop = False
            self._schedule_auto_restart()
            return False

        cmd = [command] + args
        logger.info("Starting MCP server: %s [profile=%s]", " ".join(cmd), self.profile_id)

        # 新一輪啟動：清掉上一輪殘留輸出，讓 status 的 last_output 只反映本輪
        self._last_output.clear()

        argv, parent_death = child_identity.spawn_argv(command, args, env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Own session and process group (setsid in Popen's C child code; no Python preexec).
                start_new_session=True,
            )
            # _intentional_stop 要等新子行程換上 self._process 之後才清掉（中間沒有 await）。
            # 上面的 await 會讓出事件迴圈：手動 restart 時舊子行程的 drainer 可能在這時醒來，
            # 若旗標已清、self._process 又還是舊的，它會誤判「意外結束」，偷偷把退避計數加一
            # ——之後第一次真的崩潰就從第二階（5 秒）起跳（商用驗收 DATA-NMCP-004-1）。
            self._process = proc
            self._initialize_generation_changed = asyncio.Event()
            self._intentional_stop = False
            self._running = True
            self._started_at = asyncio.get_running_loop().time()
            self._active_launch = (command, _port)
            self._generation_fault = None
            self._cancel_deferred()
            # Only a process this call really spawned gets group signalling and the OS identity check.
            group = _ChildGroup(proc, proc.pid, _port) if isinstance(proc, asyncio.subprocess.Process) else None
            self._group = group
            if group is not None:
                group.sweeper = asyncio.create_task(self._sweep_after_exit(group))
                if child_identity.LINUX and not parent_death:
                    logger.warning("MCP child (pid=%s) runs without a parent-death signal %s", proc.pid,
                                   self._tag(proc, _port))
            logger.info("MCP server started (pid=%s) %s", self._process.pid, self._tag(proc, _port))

            # Start log drainer in background（同時負責偵測意外結束、排程自動重啟）
            asyncio.create_task(self._drain_logs(self._process))
            self._verifying_configuration = True
            probe_snapshot = {**cfg, "mcp_server": {**mcp_cfg, "port": _port}}
            self._configuration_task = asyncio.create_task(
                self._verify_configuration(proc, probe_snapshot, self._attempted_revision))
            return True

        except FileNotFoundError:
            logger.error("MCP server command not found: %s %s", command, self._tag(port=_port))
            self._running = False
            self._configuration_error = "child_start_failed"
            self._verifying_configuration = False
            return False
        except Exception as exc:
            logger.error("Failed to start MCP server (%s) %s", type(exc).__name__, self._tag(port=_port))
            self._running = False
            self._configuration_error = "child_start_failed"
            self._verifying_configuration = False
            return False

    async def _port_refusal(self, port: int) -> tuple[str, str] | None:
        """Why the child must not be started on ``127.0.0.1:port`` (error code, log text), or None."""
        if port in await asyncio.to_thread(child_identity.admin_listener_ports):
            return CHILD_PORT_CONFLICT, "the admin listener uses this port"
        from .profiles import current_profile_id, get_profile_registry
        conflict = await get_profile_registry().port_conflict(current_profile_id(), port, self)
        if conflict is not None:
            return CHILD_PORT_CONFLICT, conflict
        occupied = child_identity.port_occupied(port)
        if occupied is not None:
            return CHILD_PORT_IN_USE, "another socket is bound to this port: " + occupied
        return None

    async def stop(self) -> None:
        store = get_config_store()
        if store._transition_owner is asyncio.current_task():
            await self._stop_locked()
            return
        async with store.transition():
            snapshot = await store.load()
            revision = store.revision(snapshot)
            ticket = self.prepare_initialize_drain()
        try:
            await self.drain_initializes(ticket)
            async with store.transition():
                if store.revision(await store.load()) != revision:
                    raise RuntimeError("MCP configuration changed during stop")
                self.validate_initialize_drain(ticket)
                await self._stop_locked()
        finally:
            self.release_initialize_drain(ticket)

    async def _stop_locked(self) -> None:
        """Gracefully stop the MCP server and every process left in its process group."""
        # 主動停止：不要讓看門狗把它拉回來，也取消已排程的自動重啟
        self._intentional_stop = True
        self._cancel_auto_restart()
        self._invalidate_initialize_generation()
        self._cancel_configuration_probe()
        self._applied_revision = None
        await self._quiesce_initialize_dispatches()
        self._cancel_deferred()
        proc = self._process
        group = self._group if self._group is not None and self._group.proc is proc else None
        if not proc or proc.returncode is not None:
            self._running = False
            if group is not None:
                await self._await_group_sweep(group)
            return

        logger.info("Stopping MCP server (pid=%s) %s", proc.pid, self._tag(proc))
        await self._terminate(proc, group, STOP_TIMEOUT)
        self._running = False
        logger.info("MCP server stopped %s", self._tag(proc))

    async def _terminate(self, proc, group: _ChildGroup | None, timeout: float) -> None:
        """SIGTERM (the whole group for a spawned child), SIGKILL after *timeout*, then sweep the group."""
        if group is None:
            # Not spawned by this manager (synthetic test double): signal only that object, as before.
            try:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning("MCP server did not stop in %ss, killing %s", timeout, self._tag(proc))
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
            return
        group.stopping = True
        child_identity.signal_group(proc, group.pgid, signal.SIGTERM)
        if not await _leader_exit(proc, timeout):
            logger.warning("MCP server did not stop in %ss, killing %s", timeout, self._tag(proc, group.port))
            child_identity.signal_group(proc, group.pgid, signal.SIGKILL)
            await _leader_exit(proc, None)
        await self._await_group_sweep(group)

    async def _await_group_sweep(self, group: _ChildGroup) -> None:
        sweeper = group.sweeper
        if sweeper is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(sweeper), GROUP_GRACE_SECONDS + GROUP_KILL_WAIT_SECONDS + 1)
        except asyncio.TimeoutError:
            logger.error("MCP child process group %s did not clear in time %s", group.pgid,
                         self._tag(group.proc, group.port))
        except Exception:
            pass

    async def _sweep_after_exit(self, group: _ChildGroup) -> None:
        """Once the leader was reaped, make sure no member of its process group survives (incl. a crash)."""
        await _leader_exit(group.proc, None)
        loop = asyncio.get_running_loop()
        grace_until = loop.time() + (GROUP_GRACE_SECONDS if group.stopping and child_identity.LINUX else 0.0)
        killed_at = None
        while True:
            now = loop.time()
            kill = killed_at is None and now >= grace_until
            # Scan and signal in one synchronous step: a live member pins the PGID (no reuse possible).
            sweep = child_identity.sweep_group(group.pgid, group.proc.pid, signal.SIGKILL if kill else None)
            if not sweep.live:
                return
            if sweep.signalled and killed_at is None:
                killed_at = now
                logger.warning("MCP child process group %s: %d process(es) outlived the child; sent SIGKILL %s",
                               group.pgid, len(sweep.live), self._tag(group.proc, group.port))
            elif killed_at is not None and now - killed_at >= GROUP_KILL_WAIT_SECONDS:
                logger.error("MCP child process group %s: %d process(es) still present after SIGKILL %s",
                             group.pgid, len(sweep.live), self._tag(group.proc, group.port))
                return
            await asyncio.sleep(0.05)

    async def restart(self) -> bool:
        """Stop then start the MCP server.  Returns True if restarted."""
        async with get_config_store().transition():
            try:
                await self.stop()
            except InitializeDrainPending:
                self._defer_until_drained("restart")
                raise
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
        late = self._late_verify_task
        if late is not None and not late.done() and late is not asyncio.current_task():
            late.cancel()
        self._late_verify_task = None

    @property
    def late_verification_pending(self) -> bool:
        """The probe window ended without a verdict and the background identity check is still running."""
        task = self._late_verify_task
        return task is not None and not task.done()

    async def _verify_configuration(self, proc, snapshot, revision, late: bool = False) -> None:
        """Read-only MCP readiness, bound to the exact process and launch snapshot.

        A successful spawn is pending until initialize/list works. This says
        nothing about backend credentials or a tool's ability to perform work.
        Before the first probe byte and again after the probe, every LISTEN socket that would accept a
        connection to 127.0.0.1:<port> must belong to the child's session; otherwise the generation is refused.

        When the probe window (``configuration_probe_timeout``) ends while the child is alive, the same checks
        continue in the background (*late*, no deadline, backoff up to ``late_verify_max_delay``) until the
        generation is verified, replaced, or the child exits; success makes exactly the transitions of the
        in-time path. A slow-starting child is then forwarded to, with no relaxed identity rule.
        """
        from .capabilities import discover_tools
        loop = asyncio.get_running_loop()
        deadline = None if late else loop.time() + self.configuration_probe_timeout
        delay = PEER_POLL_SECONDS

        def left() -> float:
            return float("inf") if deadline is None else deadline - loop.time()

        def pause() -> float:
            nonlocal delay
            if not late:
                return min(PEER_POLL_SECONDS, max(0, left()))
            current, delay = delay, min(delay * 2, self.late_verify_max_delay)
            return current

        changed = self._initialize_generation_changed
        group = self._group if self._group is not None and self._group.proc is proc else None
        port = coerce_port((snapshot.get("mcp_server") or {}).get("port", 8000))
        try:
            while proc is self._process and proc.returncode is None:
                if late and (changed is not self._initialize_generation_changed or changed.is_set()):
                    return  # replaced or fenced: whoever did that owns the state now
                remaining = left()
                if remaining <= 0:
                    break
                report = await self._peer_report(group, port)
                if report is not None and report.verdict is Peer.NOT_LISTENING:
                    await asyncio.sleep(pause())
                    continue
                if report is not None and report.verdict is Peer.FOREIGN:
                    await self._fail_generation(group, changed, CHILD_PEER_MISMATCH, report)
                    return
                if (report is not None and proc is self._process and proc.returncode is None
                        and changed is self._initialize_generation_changed and not changed.is_set()):
                    # The child owns every listener behind 127.0.0.1:port: MCP requests may reach it from now on
                    # (an initialize still waits for this probe to finish).
                    self._peer_verified = (changed, port)
                try:
                    async with asyncio.timeout(min(remaining, self.configuration_probe_timeout)):
                        await discover_tools(snapshot)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(max(pause(), 0.25) if late else min(0.25, max(0, left())))
                    continue
                report = await self._peer_report(group, port)
                if report is not None and report.verdict in (Peer.FOREIGN, Peer.NOT_LISTENING):
                    code = CHILD_PEER_MISMATCH if report.verdict is Peer.FOREIGN else CHILD_PEER_LOST
                    await self._fail_generation(group, changed, code, report)
                    return
                if (proc is self._process and proc.returncode is None and self._attempted_revision == revision
                        and changed is self._initialize_generation_changed and not changed.is_set()):
                    # A process this manager did not spawn (report None) has no verifiable identity and is
                    # never routable, even when its probe answers.
                    self._applied_revision = revision
                    self._configuration_error = None
                    if late:
                        # failed/late_verification → applied had no log line (2026-11-13 mp-odoo-res R7).
                        logger.info("MCP child passed its identity and readiness check after the probe window (late "
                                    "verification): applied, MCP requests are forwarded to it %s",
                                    self._tag(proc, port))
                return
            if proc is self._process and proc.returncode is None:
                self._configuration_error = "child_probe_failed"
                background = (not late and group is not None and changed is self._initialize_generation_changed
                              and not changed.is_set())
                # Previously silent (2026-11-13 mp-odoo-res R7: 30 s of failed/late_verification, 13 client 503s and
                # nothing in kubectl logs). Whether requests are forwarded meanwhile depends on the listener check.
                logger.warning("MCP child did not pass its readiness check within %gs (child_probe_failed); %s; %s %s",
                               self.configuration_probe_timeout,
                               "its listener is verified, so MCP requests are forwarded"
                               if self.forward_generation(port) is not None
                               else "no MCP request is forwarded until a check passes",
                               "the check continues in the background (late verification)" if background
                               else "no background check runs", self._tag(proc, port))
                if background:
                    # Not routable yet, but alive: keep checking identity in the background (it is stopped by
                    # stop/restart/exit/generation change) instead of staying unroutable until a manual restart.
                    self._late_verify_task = asyncio.create_task(
                        self._verify_configuration(proc, snapshot, revision, late=True))
        finally:
            current = asyncio.current_task()
            if self._configuration_task is current:
                self._verifying_configuration = False
                self._configuration_task = None
            if self._late_verify_task is current:
                self._late_verify_task = None

    async def _peer_report(self, group: _ChildGroup | None, port: int):
        """OS view of who listens on 127.0.0.1:*port* for *group*; None when the process was not spawned here."""
        if group is None:
            return None
        report = await asyncio.to_thread(child_identity.listener_report, port, group.pgid)
        if report.verdict is Peer.FOREIGN:
            # A holder can exit between the /proc reads: confirm once before refusing a generation.
            await asyncio.sleep(0.05)
            report = await asyncio.to_thread(child_identity.listener_report, port, group.pgid)
        if report.verdict is Peer.UNSUPPORTED and not self._peer_unsupported_logged:
            self._peer_unsupported_logged = True
            logger.warning("Cannot verify which process listens on the MCP child port (no /proc/net/tcp); "
                           "forwarding to 127.0.0.1:%s without an OS identity check", port)
        return report

    async def _fail_generation(self, group: _ChildGroup, changed: asyncio.Event, code: str, report) -> None:
        """Never use this generation: fence it, record *code*, stop the spawned child (the watchdog retries)."""
        proc = group.proc
        if proc is not self._process or changed is not self._initialize_generation_changed:
            return
        if self._configuration_task is asyncio.current_task():
            # The probe has decided: it is no longer verifying, and the child's exit must not cancel it.
            self._configuration_task = None
            self._verifying_configuration = False
        if self._late_verify_task is asyncio.current_task():
            self._late_verify_task = None  # same: terminating the child must not cancel the task doing it
        logger.error("MCP child (pid=%s) refused (%s): the listener on 127.0.0.1:%s is not owned by the child's "
                     "process session (%s); stopping it, no MCP request is forwarded to that port %s",
                     proc.pid, code, group.port, report.describe(), self._tag(proc, group.port))
        self._configuration_error = code
        self._generation_fault = (proc, code)
        self._applied_revision = None
        self._invalidate_initialize_generation()
        await self._terminate(proc, group, REFUSED_STOP_TIMEOUT)

    async def peer_unreachable(self, generation, port: int) -> None:
        """A forward to a verified generation failed to connect: re-check who owns the port (deduplicated).

        The child no longer owning a listener (or someone else owning one) fences the generation and stops the
        child before another process can take the port over; the watchdog then starts a new verified child.
        """
        proc, changed = generation
        group = self._group
        if (group is None or group.proc is not proc or changed is not self._initialize_generation_changed
                or changed.is_set() or self._peer_verified is None):
            return
        task = self._peer_recheck
        if task is None or task.done():
            task = self._peer_recheck = asyncio.create_task(self._recheck_peer(group, changed, port))
        try:
            await asyncio.wait_for(asyncio.shield(task), PEER_RECHECK_WAIT)
        except (asyncio.TimeoutError, Exception):
            pass

    async def _recheck_peer(self, group: _ChildGroup, changed: asyncio.Event, port: int) -> None:
        report = await self._peer_report(group, port)
        if report is None or report.verdict in (Peer.OWNED, Peer.UNSUPPORTED):
            return
        if changed.is_set() or self._peer_verified is None or self._peer_verified[0] is not changed:
            return
        await self._fail_generation(group, changed, CHILD_PEER_LOST, report)

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
            async for line, dropped in _child_output_lines(proc.stdout):
                text = line.decode("utf-8", errors="replace").rstrip()
                if dropped:
                    # 一行超過上限：留前段、標上丟掉多少。標記前有一個空白，前段結尾若是 token 的一部分，遮罩到空白就結束。
                    text += f" …[truncated {dropped} bytes]"
                if text:
                    self._last_output.append(text)
                    logger.info("[mcp-server] %s", text, extra={CHILD_OUTPUT_ATTR: True})
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
        self._invalidate_initialize_generation()
        self._running = False
        self._applied_revision = None
        self._cancel_configuration_probe()
        if self._intentional_stop:
            logger.info("MCP server exited with code %s (stopped) %s", rc, self._tag(proc))
            return
        fault = self._generation_fault
        if fault is not None and fault[0] is proc:
            logger.warning("MCP server exited with code %s after it was refused (%s) %s", rc, fault[1], self._tag(proc))
            self._configuration_error = fault[1]
        else:
            logger.warning("MCP server exited unexpectedly with code %s %s", rc, self._tag(proc))
            self._configuration_error = "child_exited"
        self._schedule_auto_restart()

    def _schedule_auto_restart(self) -> None:
        current = self._auto_restart_task
        # The running attempt itself may schedule the next one (a refused port); anyone else waits for it.
        if current is not None and not current.done() and current is not asyncio.current_task():
            return
        loop = asyncio.get_running_loop()
        if self._started_at is not None and loop.time() - self._started_at >= self.stable_seconds:
            self._consecutive_failures = 0
        delays = self.auto_restart_delays
        delay = delays[min(self._consecutive_failures, len(delays) - 1)]
        self._consecutive_failures += 1
        self._next_auto_restart_delay = delay
        self._next_auto_restart_at = loop.time() + delay
        logger.warning("MCP server auto-restart scheduled in %ss (attempt %d) %s", delay, self._consecutive_failures,
                       self._tag())
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
            logger.warning("MCP server auto-restarting (auto_restart_count=%d) %s", self._auto_restart_count,
                           self._tag())
            try:
                started = await self.start()
            except InitializeDrainPending:
                logger.warning("MCP server auto-restart waits until the previous generation's initialize dispatch ends %s",
                               self._tag())
                return
            if not started and self._auto_restart_task is asyncio.current_task():
                logger.error("MCP server auto-restart failed to spawn; giving up until next manual start %s",
                             self._tag())


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_instance: McpProcessManager | None = None
_active_process: contextvars.ContextVar[McpProcessManager | None] = contextvars.ContextVar("mcp_profile_process", default=None)


def get_default_process_manager() -> McpProcessManager:
    """Return the legacy child manager regardless of request context."""
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = McpProcessManager()
    return _instance


def get_process_manager() -> McpProcessManager:
    """Return the selected profile's child manager or the legacy singleton."""
    return _active_process.get() or get_default_process_manager()
