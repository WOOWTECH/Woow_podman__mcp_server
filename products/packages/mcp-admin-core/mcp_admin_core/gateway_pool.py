"""閘道（``proxy.py``）到 MCP 子行程的連線：每個帳號（profile）各自的連線與串流上限。

背景（2026-11-13 多帳號實測 QA-1，P2）：proxy 只建一個 ``httpx.AsyncClient``，httpx 預設最多 100 條連線，
``Timeout(86400, connect=10)`` 又讓「等連線池」也是 86400 秒。每條開著的 GET 串流（Streamable HTTP 的
``GET /mcp``、legacy SSE 的 ``GET /sse``）整段期間都佔著一條連線，所以任何一個帳號開滿 100 條串流（或同一台
累計 100 個開著串流的用戶端），整台所有帳號的 MCP 請求都卡在等連線池，``/healthz`` 卻仍是 200。

做法（:class:`ProfileLimitedTransport`，包在共用 client 的 transport 外面）：

- 仍是一個 httpx 連線池（閒置連線可以互相回收；刪掉的帳號不會留下 socket），但每個送往子行程的請求先在
  「自己帳號」的名額內取得一條連線：每帳號最多 :attr:`GatewayLimits.max_connections`（預設 64），回應關閉
  （讀完、轉發結束、用戶端斷線、token 換掉）時才歸還。共用池的總上限是「帳號數上限＋1」份，所以一個帳號用滿
  自己的名額時，其他帳號照樣拿得到連線。
- 等名額最多 :attr:`GatewayLimits.pool_timeout` 秒（預設 10）：逾時拋 :class:`GatewayBusy`（``httpx.PoolTimeout``
  的子類別），閘道回 JSON-RPC 錯誤或 503＋Retry-After，不再無限等。httpx 自己的 pool 逾時也是這個值（保險）。
- GET（串流）另有每帳號 :attr:`GatewayLimits.max_streams`（預設 32）的上限，一定小於連線上限，所以串流開滿時
  POST 仍有名額；超過時不等，立刻拋 :class:`StreamLimitReached`，閘道回 429＋Retry-After 與說明。

排隊（2026-11-13 補測與 #145 審查的後續）：

- **排隊數有上限**：名額用滿時，請求在 transport 前排隊，而排隊時請求本體（最多 16 MiB 一筆）已經讀進記憶體（實測占用約 2.5 倍，
  轉給子行程後子行程再多占約 3 倍；上限算的是個數、不是位元組，1 GiB 的 pod 上 32 個約 4 MiB 的請求就可能 OOM，見
  ``docs/mcp-gateway-limits.md`` 的「已知限制與後續」）。每個佇列
  （一般名額、通知保留名額各一個，各自計數）最多 :attr:`GatewayLimits.max_queue` 個請求同時排隊（預設 32，
  ``MCP_GATEWAY_MAX_QUEUE_PER_PROFILE``），所以每個帳號最多 2 倍；再多的請求不排隊、立刻拋 :class:`GatewayQueueFull`
  （:class:`GatewayBusy` 的子類別，回應與等名額逾時相同），記一行 WARNING。不合計成每帳號一個上限：那會讓一般排隊已滿時，
  連通知也被拒絕，破壞保留名額的目的。
- **排隊中的請求會看用戶端還在不在**：proxy 在送往子行程的請求上附 :class:`GatewayHints`（``request.extensions``），其中的
  ``gone`` 是「用戶端斷線」的等待函式。只有排隊期間才會呼叫它：用戶端在排隊時把那個 POST 的連線關掉（TCP 斷線，或 HTTP 用戶端
  因逾時、被中止而關連線），請求就丟掉、拋 :class:`ClientAbandoned`，絕不送給子行程——否則名額一空出來，用戶端已經不要的寫入
  照樣被執行，用戶端重試又寫一次（補測 Q6：3/3 寫進 Odoo）。已經取得名額、送出去的請求維持原來的語意（子行程照樣處理）。
  只認「那個 POST 的連線被關掉」，下面兩種這裡看不到、不在保護範圍：(1) MCP SDK 層級的請求逾時與取消只送
  ``notifications/cancelled``、不關閉連線；(2) MCP Python SDK 2.3.0 預設 ``terminate_on_close=True``：離開 transport 時先送
  DELETE 並等回應，才取消 POST，而 DELETE（沒有 JSON-RPC 內容）走一般名額、名額被佔滿時排在這個 POST 後面，POST 的連線一直開著，
  排隊中的寫入照樣被執行（沙盒與本機實測；``terminate_on_close=False`` 與原始 TCP 關閉則會被丟掉）。見
  ``docs/mcp-gateway-limits.md`` 的「已知限制與後續」。
- **通知與回應另有保留名額**：沒有 id 的通知（``notifications/initialized``、``cancelled``…）與用戶端對伺服器請求的回應
  （POST，裡面沒有任何請求）由 proxy 標成 ``GatewayHints.notification``，走每帳號另外
  :attr:`GatewayLimits.notification_reserve`（:data:`NOTIFICATION_RESERVE`）條的保留名額，不被一般請求的排隊擠掉。
  理由見 :class:`GatewayHints`。

帳號由 ``ProfileContextMiddleware`` 依網址 token 選定（contextvar，與 ``get_config_store()``／``get_process_manager()``
同一個來源）；沒有經過該 middleware 的呼叫算預設帳號。名額用滿時記一行 WARNING（容器 stdout＋Logs 頁），
每個帳號每種情況每 60 秒最多一行，其餘計數後在下一行以 ``suppressed=N`` 補上；不含 token、網址或請求內容。

環境變數（不合法或超出範圍就用預設值並記一行 WARNING）：

- ``MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE``：每帳號同時送往子行程的請求（連線）上限，預設 64，範圍 2～1024。
- ``MCP_GATEWAY_MAX_STREAMS_PER_PROFILE``：每帳號同時開著的 GET 串流上限，預設 32，範圍 1～連線上限−1。
- ``MCP_GATEWAY_POOL_TIMEOUT_SECONDS``：等名額的上限（秒），預設 10，範圍大於 0、最多 300。
- ``MCP_GATEWAY_MAX_QUEUE_PER_PROFILE``：每個佇列（一般名額、通知保留名額各一個）同時排隊等名額的請求數上限，預設 32，範圍 0～1024
  （0＝不排隊，名額滿就拒絕）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from .owner_defaults import _StdoutHandler
from .profiles import DEFAULT_PROFILE, MAX_EXTRA_PROFILES, current_profile_id

logger = logging.getLogger(__name__)
# 與 origin_security／initial_token 相同：admin 沒設 root logger，所以自己掛 stdout handler（kubectl logs 才看得到），
# 紀錄照常往上傳給 mcp_admin_core 的 Logs 頁 handler。訊息不含 token、網址或請求內容。
if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
    logger.addHandler(_StdoutHandler())

CONNECTIONS_ENV = "MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE"
STREAMS_ENV = "MCP_GATEWAY_MAX_STREAMS_PER_PROFILE"
POOL_TIMEOUT_ENV = "MCP_GATEWAY_POOL_TIMEOUT_SECONDS"
QUEUE_ENV = "MCP_GATEWAY_MAX_QUEUE_PER_PROFILE"

DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_MAX_STREAMS = 32
DEFAULT_POOL_TIMEOUT = 10.0
#: 每個佇列（一般名額、通知保留名額各一個）同時排隊等名額的請求數；每帳號最多 2 倍。排隊中的請求本體已在記憶體（最多 16 MiB 一筆，
#: 一般工具呼叫只有幾 KB）。這個數字限制的是個數、不是位元組：實測每個約 4 MiB 的排隊請求在閘道占約 2.5 倍、轉給子行程後再多占約
#: 3 倍，1 GiB 的 pod 上 32 個就可能 OOM（docs/mcp-gateway-limits.md「已知限制與後續」）。
DEFAULT_MAX_QUEUE = 32
MAX_CONNECTIONS_CEILING = 1024
POOL_TIMEOUT_CEILING = 300.0
MAX_QUEUE_CEILING = 1024
#: 每帳號另外保留給通知與回應的連線數（不佔一般名額）。通知的處理只是子行程回 202，名額幾乎立刻歸還，所以不需要多。
NOTIFICATION_RESERVE = 8

#: 名額用滿的 WARNING 每個帳號、每種情況最多這麼久一行。
REPORT_INTERVAL_SECONDS = 60.0
_MAX_REPORT_KEYS = 64

#: ``httpx.Request.extensions`` 裡放 :class:`GatewayHints` 的鍵。
HINTS_KEY = "woow_gateway_hints"


@dataclass(frozen=True)
class GatewayLimits:
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    max_streams: int = DEFAULT_MAX_STREAMS
    pool_timeout: float = DEFAULT_POOL_TIMEOUT
    max_queue: int = DEFAULT_MAX_QUEUE
    notification_reserve: int = NOTIFICATION_RESERVE

    @property
    def shared_pool_size(self) -> int:
        """共用 httpx 連線池的總上限：每個可能同時存在的帳號（預設＋額外帳號上限）一份（一般名額加通知保留名額），
        再多一份給剛刪掉、串流還在收尾的帳號。各帳號的名額加總不會超過它，所以沒有帳號會因為別的帳號而等不到連線。"""
        return (self.max_connections + self.notification_reserve) * (MAX_EXTRA_PROFILES + 2)

    @classmethod
    def from_env(cls) -> "GatewayLimits":
        connections = _int_env(CONNECTIONS_ENV, DEFAULT_MAX_CONNECTIONS, 2, MAX_CONNECTIONS_CEILING)
        streams = _int_env(STREAMS_ENV, min(DEFAULT_MAX_STREAMS, connections - 1), 1, connections - 1)
        timeout = _float_env(POOL_TIMEOUT_ENV, DEFAULT_POOL_TIMEOUT, POOL_TIMEOUT_CEILING)
        queue = _int_env(QUEUE_ENV, DEFAULT_MAX_QUEUE, 0, MAX_QUEUE_CEILING)
        return cls(max_connections=connections, max_streams=streams, pool_timeout=timeout, max_queue=queue)


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not low <= value <= high:
        logger.warning("%s 必須是 %d～%d 的整數，改用預設值 %d", name, low, high, default)
        return default
    return value


def _float_env(name: str, default: float, high: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or not 0 < value <= high:  # NaN 也不在範圍內
        logger.warning("%s 必須大於 0、最多 %g 秒，改用預設值 %g", name, high, default)
        return default
    return value


class GatewayBusy(httpx.PoolTimeout):
    """這個帳號的連線名額在 ``pool_timeout`` 秒內都沒有空出來（請求沒有送出）。

    ``waited``：實際等了幾秒（proxy 自己的期限比 ``pool_timeout`` 短時，例如啟動中的 initialize）；省略就是 ``pool_timeout``。

    ``connections``、``reserved``：逾時的是哪一組名額——一般名額（``limits.max_connections`` 條）或通知保留名額
    （``limits.notification_reserve`` 條，``reserved=True``）。說明文字寫那一組自己的條數；省略就是一般名額。
    """

    def __init__(self, limits: GatewayLimits, *, request: httpx.Request | None = None,
                 waited: float | None = None, connections: int | None = None, reserved: bool = False) -> None:
        self.waited = limits.pool_timeout if waited is None else waited
        self.connections = limits.max_connections if connections is None else connections
        self.reserved = reserved
        super().__init__(
            f"all {self.connections} gateway connections {'reserved for notifications ' if reserved else ''}"
            f"of this MCP profile stayed busy for {self.waited:g} s",
            request=request,
        )
        self.limits = limits


class GatewayQueueFull(GatewayBusy):
    """這一組名額（一般或通知保留）的排隊已經有 ``max_queue`` 個請求在等（這個請求沒有排隊、也沒有送出）。

    ``connections``、``reserved``：同 :class:`GatewayBusy`（排隊已滿的是哪一組名額）。
    """

    def __init__(self, limits: GatewayLimits, *, request: httpx.Request | None = None,
                 connections: int | None = None, reserved: bool = False) -> None:
        httpx.PoolTimeout.__init__(
            self,
            f"{limits.max_queue} requests of this MCP profile are already waiting for a gateway connection",
            request=request,
        )
        self.limits = limits
        self.waited = 0.0
        self.connections = limits.max_connections if connections is None else connections
        self.reserved = reserved


class StreamLimitReached(httpx.TransportError):
    """這個帳號已經開著 ``max_streams`` 條 GET 串流（請求沒有送出）。"""

    def __init__(self, limits: GatewayLimits, *, request: httpx.Request | None = None) -> None:
        super().__init__(
            f"this MCP profile already has {limits.max_streams} open event streams", request=request,
        )
        self.limits = limits


class ClientAbandoned(Exception):
    """請求在排隊等名額時，用戶端已經把那個 POST 的連線關掉了（TCP 斷線，或 HTTP 用戶端因逾時、被中止而關連線）：請求沒有送出，
    也不會再送。MCP Python SDK 預設先送 DELETE 再等回應才關的路徑、SDK 層級的逾時與取消都不是關連線，不會走到這裡（見模組說明）。

    不是 ``httpx`` 的例外：不屬於子行程或連線的錯誤，proxy 單獨接住、回 499。沒有人收得到這個 499：用戶端已經斷線，uvicorn 也不記
    access 行（沙盒與本機實測 0 行 499），只有 transport 記的那一行 WARNING 看得出來。
    """


@dataclass(eq=False)
class GatewayHints:
    """proxy 附在送往子行程的請求上（``request.extensions[HINTS_KEY]``）、給 :class:`ProfileLimitedTransport` 的資訊。

    - ``notification``：這個 POST 裡沒有任何 JSON-RPC 請求（只有通知與用戶端對伺服器請求的回應），走保留名額。
      為什麼不和一般請求一樣在忙碌時回 503：各 SDK 對通知 POST 的非 2xx 反應都很糟——MCP Python SDK 1.x 的
      ``post_writer`` 對 4xx／5xx（404 除外）``raise_for_status()`` 後整個傳輸層關閉（session 就此結束），TS SDK 的
      ``send()`` 拋錯（``connect()`` 時的 ``notifications/initialized`` 失敗就是連線失敗），Python SDK 2.x 靜默忽略（通知
      就這樣掉了）。反過來「回 202 但不轉發」會靜默掉會改變狀態的通知，而用戶端以為都送到了：``notifications/cancelled`` 掉了，
      用戶端已經放棄的請求（可能是寫入）照樣在子行程跑完；用戶端對伺服器請求（取樣、elicitation、roots）的回應掉了，子行程
      那個請求一直等；``notifications/initialized`` 掉了，SDK 2.x 子行程不會發起 server→client 請求、TS SDK 用戶端不會開
      GET 串流（它只在收到 202 時才開）；``roots/list_changed`` 掉了，子行程的 roots 過期。所以通知要真的送到子行程，一般名額
      用滿時用保留名額；保留名額也用完（子行程整個卡死才會發生）才照一般規則排隊、逾時回 503。
    - ``gone``：用戶端斷線時結束的等待函式（``proxy._client_disconnect``）。只有請求排隊等名額時才會呼叫；省略就不看。
    - ``queued``、``queued_at``、``limits``：transport 在請求排隊期間設定（proxy 在自己的期限到時，用來分辨「還在排隊」
      與「子行程沒有回應」）。
    """

    notification: bool = False
    gone: Callable[[], Awaitable[None]] | None = None
    queued: bool = False
    queued_at: float = 0.0
    limits: GatewayLimits | None = None

    def attach(self, request: httpx.Request) -> None:
        extensions = getattr(request, "extensions", None)
        if isinstance(extensions, dict):  # 測試替身的 client 可能回傳不是 httpx.Request 的物件：沒有地方放就不放
            extensions[HINTS_KEY] = self

    def waited(self) -> float:
        """排隊到現在多久（沒有在排隊時為 0）。"""
        return max(0.0, time.monotonic() - self.queued_at) if self.queued else 0.0


class _Lane:
    """一個帳號的一組名額：一般請求的，或另外保留給通知的（``reserved``）。``limit`` 是這一組自己的連線數。"""

    __slots__ = ("semaphore", "limit", "reserved", "connections", "waiting")

    def __init__(self, limit: int, *, reserved: bool = False) -> None:
        self.semaphore = asyncio.Semaphore(limit)
        self.limit = limit
        self.reserved = reserved
        self.connections = 0
        self.waiting = 0


class _Slots:
    """一個帳號目前用掉的名額。全部歸零就從表裡移除（下一個請求重新建立，不會綁住舊的事件迴圈）。"""

    __slots__ = ("main", "notify", "streams")

    def __init__(self, limits: GatewayLimits) -> None:
        self.main = _Lane(limits.max_connections)
        self.notify = _Lane(limits.notification_reserve, reserved=True)
        self.streams = 0

    @property
    def connections(self) -> int:
        return self.main.connections + self.notify.connections

    @property
    def waiting(self) -> int:
        return self.main.waiting + self.notify.waiting

    def idle(self) -> bool:
        return not (self.connections or self.waiting or self.streams)


class _ReleasingStream(httpx.AsyncByteStream):
    """子行程回應的內容；關閉（httpx ``Response.aclose``／讀完）時先關掉真正的連線，再歸還名額（只一次）。"""

    def __init__(self, inner: httpx.AsyncByteStream, release: Callable[[], None]) -> None:
        self._inner = inner
        self._release = release

    def __aiter__(self):
        return self._inner.__aiter__()

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._release()


def _hand_back(semaphore: asyncio.Semaphore) -> Callable[[asyncio.Future], None]:
    """``semaphore.acquire()`` 的 task 結束時：它若已經拿到名額、而等的人早就放棄了，就把名額還回去。"""

    def done(task: asyncio.Future) -> None:
        if not task.cancelled() and task.exception() is None:
            semaphore.release()

    return done


def _consume(task: asyncio.Future) -> None:
    """取回放棄的 task 的結果，避免「Task exception was never retrieved」。"""
    if not task.cancelled():
        task.exception()


async def _watch_client(gone: Callable[[], Awaitable[None]]) -> None:
    """等用戶端斷線。watcher 自己出錯（例如 ASGI ``receive`` 拋例外）不能被當成斷線，也不能讓排隊提早結束：
    那就停止觀察、不再結束（排隊照舊靠名額與期限）。"""
    try:
        await gone()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 見上
        await asyncio.Event().wait()


class ProfileLimitedTransport(httpx.AsyncBaseTransport):
    """在共用連線池前面，依帳號限制同時送往子行程的請求數與 GET 串流數（見模組說明）。"""

    def __init__(self, inner: httpx.AsyncBaseTransport, limits: GatewayLimits,
                 profile: Callable[[], str] = current_profile_id) -> None:
        self._inner = inner
        self.limits = limits
        self._profile = profile
        self._slots: dict[str, _Slots] = {}

    def usage(self, profile_id: str = DEFAULT_PROFILE) -> tuple[int, int]:
        """``(使用中的連線, 開著的 GET 串流)``；給測試與診斷用。"""
        slots = self._slots.get(profile_id)
        return (slots.connections, slots.streams) if slots is not None else (0, 0)

    def waiting(self, profile_id: str = DEFAULT_PROFILE) -> int:
        """這個帳號現在有幾個請求在排隊等名額；給測試與診斷用。"""
        slots = self._slots.get(profile_id)
        return slots.waiting if slots is not None else 0

    def _forget_if_idle(self, profile_id: str, slots: _Slots) -> None:
        if slots.idle() and self._slots.get(profile_id) is slots:
            del self._slots[profile_id]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        profile_id = self._profile() or DEFAULT_PROFILE
        hints = request.extensions.get(HINTS_KEY)
        if not isinstance(hints, GatewayHints):
            hints = None
        slots = self._slots.get(profile_id)
        if slots is None:
            slots = self._slots[profile_id] = _Slots(self.limits)
        stream = request.method == "GET"
        lane = slots.main
        if hints is not None and hints.notification and not stream and self.limits.notification_reserve > 0:
            lane = slots.notify
        if stream:
            if slots.streams >= self.limits.max_streams:
                self._forget_if_idle(profile_id, slots)
                _report(profile_id, "streams",
                        "這個帳號開著的 GET 串流已達上限 %d 條，新的串流回 429（%s）",
                        self.limits.max_streams, STREAMS_ENV)
                raise StreamLimitReached(self.limits, request=request)
            slots.streams += 1
        acquired = False
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            if acquired:
                lane.connections -= 1
                lane.semaphore.release()
            if stream:
                slots.streams -= 1
            self._forget_if_idle(profile_id, slots)

        try:
            await self._acquire(profile_id, lane, request, hints)
            acquired = True
            lane.connections += 1
            response = await self._inner.handle_async_request(request)
        except BaseException:
            release()
            raise
        if response.is_closed:
            # 內容已經整個讀完（不是串流，例如測試的 MockTransport）：沒有佔著連線，馬上歸還。
            release()
        else:
            response.stream = _ReleasingStream(response.stream, release)
        return response

    async def _acquire(self, profile_id: str, lane: _Lane, request: httpx.Request,
                       hints: GatewayHints | None) -> None:
        """取得 *lane* 的一個名額：有空位就直接拿；沒有就排隊（數量有上限、時間有上限、用戶端走了就放棄）。

        取得名額之後到呼叫端記下 ``acquired`` 之間沒有任何 ``await``：不論之後在哪裡被取消，名額都不會漏掉。
        """
        if not lane.semaphore.locked():
            await lane.semaphore.acquire()  # 有空位：不排隊、不讓出事件迴圈
            return
        if lane.waiting >= self.limits.max_queue:
            _report(profile_id, "queue",
                    "這個帳號等名額的請求已有 %d 個，達到上限，新的請求不再排隊、直接拒絕（%s）",
                    self.limits.max_queue, QUEUE_ENV)
            raise GatewayQueueFull(self.limits, request=request, connections=lane.limit, reserved=lane.reserved)
        lane.waiting += 1
        if hints is not None:
            hints.queued, hints.queued_at, hints.limits = True, time.monotonic(), self.limits
        try:
            await self._wait_for_slot(profile_id, lane, request, hints)
        finally:
            lane.waiting -= 1
            if hints is not None:
                hints.queued = False

    async def _wait_for_slot(self, profile_id: str, lane: _Lane, request: httpx.Request,
                             hints: GatewayHints | None) -> None:
        loop = asyncio.get_running_loop()
        acquire = loop.create_task(lane.semaphore.acquire())
        watch = (loop.create_task(_watch_client(hints.gone))
                 if hints is not None and hints.gone is not None else None)
        keep = False
        try:
            done, _ = await asyncio.wait({acquire} if watch is None else {acquire, watch},
                                         timeout=self.limits.pool_timeout, return_when=asyncio.FIRST_COMPLETED)
            got = acquire in done and not acquire.cancelled() and acquire.exception() is None
            left = watch is not None and watch in done
            if got and not left:
                keep = True
                return
            # 用戶端同一刻也走了、名額也剛好拿到：寧可還回去也不送出（見 finally：_hand_back 會還）
            if left:
                _report(profile_id, "abandoned",
                        "這個帳號有請求在排隊等名額時用戶端已中斷連線，這些請求沒有送出")
                raise ClientAbandoned
            if lane.reserved:  # 通知保留名額：條數是固定的保留數，不是 CONNECTIONS_ENV；只有子行程幾乎不處理通知時才會用滿
                _report(profile_id, "connections",
                        "這個帳號保留給通知的 %d 條連線都在使用中，等了 %g 秒仍沒有空出來，請求沒有送出（子行程可能卡住了）",
                        lane.limit, self.limits.pool_timeout)
            else:
                _report(profile_id, "connections",
                        "這個帳號送往子行程的 %d 條連線都在使用中，等了 %g 秒仍沒有空出來，請求沒有送出（%s）",
                        lane.limit, self.limits.pool_timeout, CONNECTIONS_ENV)
            raise GatewayBusy(self.limits, request=request, connections=lane.limit, reserved=lane.reserved)
        finally:
            # 只用同步的收尾（cancel、done callback）：外面被取消時也不會在這裡再被打斷而漏掉名額。
            if not keep:
                if not acquire.done():
                    acquire.cancel()
                acquire.add_done_callback(_hand_back(lane.semaphore))
            if watch is not None:
                if not watch.done():
                    watch.cancel()
                watch.add_done_callback(_consume)

    async def aclose(self) -> None:
        await self._inner.aclose()


# 名額用滿的紀錄：(帳號, 情況) → [下一次可以記的時間, 期間略過的次數]
_reports: dict[tuple[str, str], list] = {}


def _report(profile_id: str, kind: str, message: str, *args) -> None:
    now = time.monotonic()
    key = (profile_id, kind)
    state = _reports.get(key)
    if state is not None and now < state[0]:
        state[1] += 1
        return
    suppressed = state[1] if state is not None else 0
    if len(_reports) >= _MAX_REPORT_KEYS:
        for stale in [k for k, v in _reports.items() if v[0] <= now]:
            del _reports[stale]
    _reports[key] = [now + REPORT_INTERVAL_SECONDS, 0]
    label = "default" if profile_id == DEFAULT_PROFILE else profile_id[:8]
    logger.warning("MCP 閘道 profile=%s " + message + (" suppressed=%d" if suppressed else ""),
                   label, *args, *((suppressed,) if suppressed else ()))


def report_wait_expired(limits: GatewayLimits, waited: float) -> GatewayBusy:
    """proxy 自己的期限（例如啟動中的 initialize 的 10 秒）在請求還排隊等名額時到了：記與 transport 逾時相同的 WARNING，
    回傳要拋的 :class:`GatewayBusy`（與一般「等名額逾時」同一種回應）。"""
    _report(current_profile_id() or DEFAULT_PROFILE, "connections",
            "這個帳號送往子行程的 %d 條連線都在使用中，等了 %g 秒仍沒有空出來，請求沒有送出（%s）",
            limits.max_connections, waited, CONNECTIONS_ENV)
    return GatewayBusy(limits, waited=waited)


def new_client(timeout: float, limits: GatewayLimits | None = None) -> httpx.AsyncClient:
    """proxy 的共用 client：一個連線池＋每帳號名額（``limits`` 省略時讀環境變數）。"""
    limits = limits or GatewayLimits.from_env()
    inner = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=limits.shared_pool_size))
    return httpx.AsyncClient(
        transport=ProfileLimitedTransport(inner, limits),
        timeout=httpx.Timeout(timeout, connect=10.0, pool=limits.pool_timeout),
    )
