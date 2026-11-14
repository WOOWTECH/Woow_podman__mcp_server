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

帳號由 ``ProfileContextMiddleware`` 依網址 token 選定（contextvar，與 ``get_config_store()``／``get_process_manager()``
同一個來源）；沒有經過該 middleware 的呼叫算預設帳號。名額用滿時記一行 WARNING（容器 stdout＋Logs 頁），
每個帳號每種情況每 60 秒最多一行，其餘計數後在下一行以 ``suppressed=N`` 補上；不含 token、網址或請求內容。

環境變數（不合法或超出範圍就用預設值並記一行 WARNING）：

- ``MCP_GATEWAY_MAX_CONNECTIONS_PER_PROFILE``：每帳號同時送往子行程的請求（連線）上限，預設 64，範圍 2～1024。
- ``MCP_GATEWAY_MAX_STREAMS_PER_PROFILE``：每帳號同時開著的 GET 串流上限，預設 32，範圍 1～連線上限−1。
- ``MCP_GATEWAY_POOL_TIMEOUT_SECONDS``：等名額的上限（秒），預設 10，範圍大於 0、最多 300。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

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

DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_MAX_STREAMS = 32
DEFAULT_POOL_TIMEOUT = 10.0
MAX_CONNECTIONS_CEILING = 1024
POOL_TIMEOUT_CEILING = 300.0

#: 名額用滿的 WARNING 每個帳號、每種情況最多這麼久一行。
REPORT_INTERVAL_SECONDS = 60.0
_MAX_REPORT_KEYS = 64


@dataclass(frozen=True)
class GatewayLimits:
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    max_streams: int = DEFAULT_MAX_STREAMS
    pool_timeout: float = DEFAULT_POOL_TIMEOUT

    @property
    def shared_pool_size(self) -> int:
        """共用 httpx 連線池的總上限：每個可能同時存在的帳號（預設＋額外帳號上限）一份，再多一份給剛刪掉、
        串流還在收尾的帳號。各帳號的名額加總不會超過它，所以沒有帳號會因為別的帳號而等不到連線。"""
        return self.max_connections * (MAX_EXTRA_PROFILES + 2)

    @classmethod
    def from_env(cls) -> "GatewayLimits":
        connections = _int_env(CONNECTIONS_ENV, DEFAULT_MAX_CONNECTIONS, 2, MAX_CONNECTIONS_CEILING)
        streams = _int_env(STREAMS_ENV, min(DEFAULT_MAX_STREAMS, connections - 1), 1, connections - 1)
        timeout = _float_env(POOL_TIMEOUT_ENV, DEFAULT_POOL_TIMEOUT, POOL_TIMEOUT_CEILING)
        return cls(max_connections=connections, max_streams=streams, pool_timeout=timeout)


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
    """這個帳號的連線名額在 ``pool_timeout`` 秒內都沒有空出來（請求沒有送出）。"""

    def __init__(self, limits: GatewayLimits, *, request: httpx.Request | None = None) -> None:
        super().__init__(
            f"all {limits.max_connections} gateway connections of this MCP profile stayed busy "
            f"for {limits.pool_timeout:g} s",
            request=request,
        )
        self.limits = limits


class StreamLimitReached(httpx.TransportError):
    """這個帳號已經開著 ``max_streams`` 條 GET 串流（請求沒有送出）。"""

    def __init__(self, limits: GatewayLimits, *, request: httpx.Request | None = None) -> None:
        super().__init__(
            f"this MCP profile already has {limits.max_streams} open event streams", request=request,
        )
        self.limits = limits


class _Slots:
    """一個帳號目前用掉的名額。全部歸零就從表裡移除（下一個請求重新建立，不會綁住舊的事件迴圈）。"""

    __slots__ = ("semaphore", "connections", "waiting", "streams")

    def __init__(self, limit: int) -> None:
        self.semaphore = asyncio.Semaphore(limit)
        self.connections = 0
        self.waiting = 0
        self.streams = 0

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

    def _forget_if_idle(self, profile_id: str, slots: _Slots) -> None:
        if slots.idle() and self._slots.get(profile_id) is slots:
            del self._slots[profile_id]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        profile_id = self._profile() or DEFAULT_PROFILE
        slots = self._slots.get(profile_id)
        if slots is None:
            slots = self._slots[profile_id] = _Slots(self.limits.max_connections)
        stream = request.method == "GET"
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
                slots.connections -= 1
                slots.semaphore.release()
            if stream:
                slots.streams -= 1
            self._forget_if_idle(profile_id, slots)

        try:
            slots.waiting += 1
            try:
                async with asyncio.timeout(self.limits.pool_timeout):
                    await slots.semaphore.acquire()
            except TimeoutError:
                _report(profile_id, "connections",
                        "這個帳號送往子行程的 %d 條連線都在使用中，等了 %g 秒仍沒有空出來，請求沒有送出（%s）",
                        self.limits.max_connections, self.limits.pool_timeout, CONNECTIONS_ENV)
                raise GatewayBusy(self.limits, request=request) from None
            finally:
                slots.waiting -= 1
            acquired = True
            slots.connections += 1
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


def new_client(timeout: float, limits: GatewayLimits | None = None) -> httpx.AsyncClient:
    """proxy 的共用 client：一個連線池＋每帳號名額（``limits`` 省略時讀環境變數）。"""
    limits = limits or GatewayLimits.from_env()
    inner = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=limits.shared_pool_size))
    return httpx.AsyncClient(
        transport=ProfileLimitedTransport(inner, limits),
        timeout=httpx.Timeout(timeout, connect=10.0, pool=limits.pool_timeout),
    )
