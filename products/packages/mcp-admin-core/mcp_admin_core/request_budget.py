"""閘道的「請求內容位元組預算」：限制同時握在記憶體裡的請求內容總量（大請求的記憶體保護）。

背景（沙盒 mp-retest2 T8，P2，#148 之前就有）：``gateway_pool`` 的排隊上限只算個數、不算位元組，而且已經送進子行程的請求
（連線名額內，每帳號最多 64 個）與還在讀取內容的請求完全不受限。1 GiB 的 pod 上，32 個約 4 MiB 的 ``tools/call`` 排進佇列、
放開後閘道與子行程記憶體相加就超過上限，容器 OOMKilled，整個 pod 的所有帳號一起重啟；直接送 64 個 15 MiB 的請求更不必排隊。

做法：每個請求在**開始讀內容之前**先向預算登記它宣告的大小（``Content-Length``）；沒有 ``Content-Length``（chunked）就邊讀邊登記。
預算分兩層（兩層都過才放行）：

- 全域（``MCP_GATEWAY_MAX_BUFFERED_BYTES``）：整個閘道（所有帳號）同時握著的請求內容位元組；
- 每帳號（``MCP_GATEWAY_MAX_BUFFERED_BYTES_PER_PROFILE``）：單一帳號的份額，所以一個帳號用滿自己的份額不會影響別的帳號。

超過時不排隊、不等：不保留內容（宣告大小的請求讀完丟棄後）回與「佇列已滿」一致的忙碌回應（見 ``proxy._refuse_over_budget``），並記一行節流的 WARNING。

計量的是「請求內容的位元組數」，不是記憶體本身：實測每個請求內容在閘道約占 2 倍、轉給子行程後子行程再約 3 倍（單一大字串合計約 5.6 倍），
結構化的 JSON 更高；``create_app`` 的結構守門（``request_limits.structure_violation``）把「結構」放大壓到最壞約 8 倍，但大字串裡只要
有一個非 BMP 字元（emoji），Python str 整串變 4 位元組／字元，合法 UTF-8 的 body 單純 astral 實測 15.2 倍（第二輪審查 F3），astral 加上守門上限內最壞結構疊加約 17.8 倍（第三輪審查）；所以
:data:`AMPLIFICATION` 取 20（見 ``docs/mcp-gateway-limits.md``）。預設值把這個最壞倍數（:data:`AMPLIFICATION`）、閘道基線、最大帳號數 × 子行程記憶體與安全邊際都扣掉再算
（:func:`default_total_bytes`）；環境變數的單位是請求內容位元組。

登記的生命週期：從開始讀內容起，到「送往子行程的請求的回應結束（或任何失敗）」為止。:class:`Reservation` 以兩個條件決定何時歸還：
proxy 處理完這個請求（:meth:`Reservation.finish`），**而且**沒有任何送往子行程的請求還開著（transport 對每個送出的請求 ``attach``，
回應關閉、失敗、取消時 ``detach``，與連線名額同一個時機）。所以串流回應（SSE）結束前預算一直被佔著，其他路徑（拒絕、停用的工具、
token 換掉、例外、取消）在 proxy 回應時就還回去。

不拒絕的請求：

- 小請求（登記大小不超過 :data:`SMALL_REQUEST_BYTES`，64 KiB）不計入、也不會被預算拒絕：通知（``notifications/*``）、一般幾 KB 的
  工具呼叫、initialize 都屬於這一類。保留名額的設計目的是讓通知一定送得到，位元組預算不能破壞它。小請求的總量沒有預算上限，「讀取中的請求數」也沒有上限
  （``docs/mcp-gateway-limits.md`` 已知限制 (2)）。
- 超過單筆上限（16 MiB，n8n 是 4 MiB）的請求：原本就不保留內容（只留頭尾找 id、其餘丟棄），不計入。

單一執行緒（asyncio 事件迴圈）內使用：沒有鎖、沒有 await，所以所有的計數更新都是原子的。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .launch_policy import get_launch_policy
from .profiles import DEFAULT_PROFILE, _memory_limit_mib, current_profile_capacity

from .owner_defaults import _StdoutHandler

logger = logging.getLogger(__name__)
# 與 gateway_pool／connection_apply／profiles 相同：admin 沒設 root logger，所以自己掛 stdout handler，不合法的環境變數（啟動時）與
# 預算拒絕的 WARNING 才進容器 stdout（kubectl logs），同時照常往上傳給 mcp_admin_core 的 Logs 頁 handler（第二輪 P2-C，沙盒 P3）。
# 訊息只有變數名、範圍與預設值，不含設定值、token 或請求內容。
if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
    logger.addHandler(_StdoutHandler())

TOTAL_ENV = "MCP_GATEWAY_MAX_BUFFERED_BYTES"
PER_PROFILE_ENV = "MCP_GATEWAY_MAX_BUFFERED_BYTES_PER_PROFILE"
BODY_IDLE_ENV = "MCP_GATEWAY_BODY_IDLE_SECONDS"
BODY_TOTAL_ENV = "MCP_GATEWAY_BODY_TOTAL_SECONDS"

#: 已登記的大請求（超過 :data:`SMALL_REQUEST_BYTES`）讀取內容時，連續這麼多秒沒有收到任何新位元組就放棄、歸還預算（第二輪 P2-D）。
#: 用戶端宣告很大的 ``Content-Length`` 之後不送內容（或每隔很久才送一點），預算會被鎖到連線關閉為止（uvicorn 沒有 body 讀取逾時）。
DEFAULT_BODY_IDLE_SECONDS = 30.0
MIN_BODY_IDLE_SECONDS = 1.0
MAX_BODY_IDLE_SECONDS = 3600.0

#: 已登記的大請求從開始讀內容起，整個上傳最多這麼多秒，超過就放棄、歸還預算（第三輪 F5）。閒置逾時只看「兩段之間的空檔」，每隔
#: 29 秒送 1 位元組的涓流永遠不會觸發它；整體期限擋住它。15 MiB 在 300 秒內讀完需要約 400 kbps，一般用戶端與 Cloudflare 後面的連線都夠。
DEFAULT_BODY_TOTAL_SECONDS = 300.0
MIN_BODY_TOTAL_SECONDS = 1.0
MAX_BODY_TOTAL_SECONDS = 3600.0

MIB = 1024 * 1024
#: 單筆請求內容的上限（與 request_body.MAX_REQUEST_BODY_BYTES 相同）：每個帳號任何時候至少要能放行一個這麼大的請求，
#: 所以兩個預算的下限都是它。
MIN_BUDGET_BYTES = 16 * MIB
MAX_BUDGET_BYTES = 16 * 1024 * MIB

#: 小請求（登記大小不超過這個值）不計入預算、不會被拒絕（見模組說明）。
SMALL_REQUEST_BYTES = 64 * 1024

#: 一個請求內容位元組在閘道與子行程合計占用的記憶體倍數（結構守門 ``request_limits.structure_violation`` 之下、合法 UTF-8 的最壞情況）。
#: 2026-11-15 本機實測（真的 uvicorn 閘道＋真的 MCP SDK 子行程）：16 MiB 單一大 ASCII 字串 5.5～5.6 倍；守門上限內最壞的結構（約 199,000 個
#: 元素的 ``{"a":1}`` 加大字串）8.0 倍；沙盒 Odoo 約 5.4～6.8 倍、n8n（Node）約 8.3 倍。第二輪審查 F3：大字串裡只要有一個非 BMP 字元
#: （😀，原字元或 ``\\ud83d\\ude00`` 跳脫都一樣），PEP 393 讓整個 str 變 4 位元組／字元，閘道與子行程各解析一次，實測 15.2 倍（原字元）、
#: 14.5 倍（跳脫）；CJK（BMP）是 2 位元組／字元，較輕。結構守門擋不到這一種（元素數 0）；第三輪審查實測 astral 字串加守門上限內最壞結構疊加 17.6～17.8 倍，所以取能涵蓋它的 20。
#: 正式站全是 1 GiB，預算本來就在下限 16 MiB（一個請求），提高倍數不會讓它更差，只縮小 2 GiB 以上的預算、堵住 2 GiB 的 OOM。
AMPLIFICATION = 20.0
#: 閘道自己的基線記憶體（沙盒實測約 71～76 MiB）。
GATEWAY_BASELINE_MIB = 76
#: 安全邊際：所有帳號的子行程都在跑、預算全用滿之後，仍要留給尖峰（GC、子行程暫時變大、日誌）的記憶體。
SAFETY_MARGIN_MIB = 150
#: 沒有 cgroup 記憶體上限可參考時的全域預算固定值（請求內容位元組）。沒有 cgroup 上限代表不是容器（本機、測試），不再用倍數推算；放大 20 倍最壞約 640 MiB。
FALLBACK_TOTAL_BYTES = 32 * MIB
#: 每帳號預設份額 ＝ 全域的這個比例（至少 :data:`MIN_BUDGET_BYTES`）：一個帳號最多占一半，另一個帳號仍有空間。
PER_PROFILE_FRACTION = 0.5
#: 沒有 LaunchPolicy（只有測試與自行組裝）時，每個子行程的記憶體估計（同 ``LaunchPolicy.profile_child_memory_mib`` 的預設值）。
DEFAULT_CHILD_MEMORY_MIB = 160


def default_total_bytes(memory_limit_mib: int | None, *, accounts: int = 1, child_memory_mib: int = DEFAULT_CHILD_MEMORY_MIB) -> int:
    """依容器記憶體上限、帳號數與每個子行程的記憶體推導的全域預算（請求內容位元組）；沒有上限時用固定值。

    全域預算 ＝ (cgroup 上限 − 閘道基線 − 帳號數 × 每個子行程的記憶體 − 安全邊際) ÷ 放大倍數，至少 16 MiB（單筆上限：
    每個帳號任何時候都放行得了一個最大的請求）。所有帳號的子行程都在跑時，預算用滿也不會超過上限減去安全邊際。"""
    if memory_limit_mib is None:
        return FALLBACK_TOTAL_BYTES
    available = memory_limit_mib - GATEWAY_BASELINE_MIB - accounts * child_memory_mib - SAFETY_MARGIN_MIB
    derived = int(max(0, available) * MIB / AMPLIFICATION)
    return min(MAX_BUDGET_BYTES, max(MIN_BUDGET_BYTES, derived))


def default_per_profile_bytes(total: int) -> int:
    return max(MIN_BUDGET_BYTES, int(total * PER_PROFILE_FRACTION))


def runtime_inputs(memory_limit_mib: int | None) -> tuple[int, int]:
    """(最多同時執行的帳號數, 每個子行程的記憶體估計 MiB)：來自產品的 LaunchPolicy 與容器記憶體上限（與 profiles 的容量判斷同一份）。"""
    policy = get_launch_policy()
    if policy is None:
        return 1, DEFAULT_CHILD_MEMORY_MIB
    capacity = current_profile_capacity(memory_limit_mib, policy)
    return 1 + capacity.limit, policy.profile_child_memory_mib


def _bytes_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not MIN_BUDGET_BYTES <= value <= MAX_BUDGET_BYTES:
        logger.warning("%s 必須是 %d～%d（位元組）的整數，改用預設值 %d", name, MIN_BUDGET_BYTES, MAX_BUDGET_BYTES, default)
        return default
    return value


def _seconds_env(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or not low <= value <= high:  # nan 也不會通過
        logger.warning("%s 必須是 %g～%g（秒）的數字，改用預設值 %g", name, low, high, default)
        return default
    return value


def _idle_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or not MIN_BODY_IDLE_SECONDS <= value <= MAX_BODY_IDLE_SECONDS:  # nan 也不會通過
        logger.warning("%s 必須是 %g～%g（秒）的數字，改用預設值 %g", name, MIN_BODY_IDLE_SECONDS, MAX_BODY_IDLE_SECONDS, default)
        return default
    return value


@dataclass(frozen=True)
class BudgetLimits:
    total: int
    per_profile: int
    #: 已登記的大請求讀取內容時，連續沒有新位元組多久就放棄（見 :data:`DEFAULT_BODY_IDLE_SECONDS`）。
    body_idle_seconds: float = DEFAULT_BODY_IDLE_SECONDS
    #: 已登記的大請求整個上傳的期限（見 :data:`DEFAULT_BODY_TOTAL_SECONDS`）。
    body_total_seconds: float = DEFAULT_BODY_TOTAL_SECONDS

    @classmethod
    def from_env(cls, *, accounts: int | None = None, child_memory_mib: int | None = None) -> "BudgetLimits":
        """環境變數優先；沒設就依容器記憶體上限、帳號數、子行程記憶體推導（*accounts*、*child_memory_mib* 省略時取產品的 LaunchPolicy）。"""
        ceiling = _memory_limit_mib()
        if accounts is None or child_memory_mib is None:
            policy_accounts, policy_child = runtime_inputs(ceiling)
            accounts = policy_accounts if accounts is None else accounts
            child_memory_mib = policy_child if child_memory_mib is None else child_memory_mib
        total = _bytes_env(TOTAL_ENV, default_total_bytes(ceiling, accounts=accounts, child_memory_mib=child_memory_mib))
        per_profile = _bytes_env(PER_PROFILE_ENV, default_per_profile_bytes(total))
        return cls(total=total, per_profile=per_profile, body_idle_seconds=_idle_env(BODY_IDLE_ENV, DEFAULT_BODY_IDLE_SECONDS),
                   body_total_seconds=_seconds_env(BODY_TOTAL_ENV, DEFAULT_BODY_TOTAL_SECONDS, MIN_BODY_TOTAL_SECONDS, MAX_BODY_TOTAL_SECONDS))


class BudgetExceeded(Exception):
    """這個請求的內容放不進預算（沒有讀、也不會送出）。``scope``：``"global"`` 或 ``"profile"``；``head`` 是已經讀到的開頭（找 id 用）。"""

    def __init__(self, scope: str, limit: int, *, head: bytes = b"") -> None:
        super().__init__(f"request body budget exceeded ({scope}, limit {limit} bytes)")
        self.scope = scope
        self.limit = limit
        self.head = head


class BodyIdleTimeout(Exception):
    """已登記預算的請求在讀取內容時，連續 ``seconds`` 秒沒有收到任何新位元組（慢速連線／宣告很大卻不送內容）。"""

    def __init__(self, seconds: float, *, total: bool = False) -> None:
        super().__init__(f"request body {'not complete after' if total else 'idle for'} {seconds:g} seconds")
        self.seconds = seconds
        #: True＝整個上傳超過整體期限（不是閒置）。
        self.total = total


class RequestBudget:
    """全域與每帳號的請求內容位元組預算。"""

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits = limits or BudgetLimits.from_env()
        self._total = 0
        self._profiles: dict[str, int] = {}
        #: 歷來最高的全域登記量（給測試與診斷用）
        self.peak = 0

    def used(self, profile_id: str | None = None) -> int:
        """目前登記中的位元組（全域，或某個帳號）；給測試與診斷用。"""
        return self._total if profile_id is None else self._profiles.get(profile_id, 0)

    def open(self, profile_id: str = DEFAULT_PROFILE) -> "Reservation":
        return Reservation(self, profile_id)

    def _charge(self, profile_id: str, amount: int) -> None:
        """登記 *amount* 位元組；放不下就拋 :class:`BudgetExceeded`（不改任何計數）。"""
        profile_used = self._profiles.get(profile_id, 0)
        if profile_used + amount > self.limits.per_profile:
            raise BudgetExceeded("profile", self.limits.per_profile)
        if self._total + amount > self.limits.total:
            raise BudgetExceeded("global", self.limits.total)
        self._total += amount
        self.peak = max(self.peak, self._total)
        self._profiles[profile_id] = profile_used + amount

    def _refund(self, profile_id: str, amount: int) -> None:
        self._total -= amount
        left = self._profiles.get(profile_id, 0) - amount
        if left > 0:
            self._profiles[profile_id] = left
        else:
            self._profiles.pop(profile_id, None)


class Reservation:
    """一個請求登記的位元組。``size`` 是目前讀到（或宣告）的內容大小，``charged`` 是實際計入預算的量（小請求與超過單筆上限的為 0）。"""

    __slots__ = ("_budget", "profile_id", "size", "charged", "_refs", "_finished", "_closed")

    def __init__(self, budget: RequestBudget, profile_id: str) -> None:
        self._budget = budget
        self.profile_id = profile_id
        self.size = 0
        self.charged = 0
        self._refs = 0
        self._finished = False
        self._closed = False

    def grow(self, size: int, *, cap: int) -> None:
        """內容大小變成 *size*（只增不減）。超過 *cap*（單筆上限）的請求之後會被丟棄內容，把已登記的還回去、不再計。

        放不下拋 :class:`BudgetExceeded`（已登記的部分原樣保留，由 :meth:`finish` 歸還）。"""
        if self._closed or size <= self.size:
            return
        self.size = size
        if size > cap:
            self._refund_all()
        elif size > SMALL_REQUEST_BYTES and size > self.charged:
            self._budget._charge(self.profile_id, size - self.charged)
            self.charged = size

    def _refund_all(self) -> None:
        if self.charged:
            self._budget._refund(self.profile_id, self.charged)
            self.charged = 0

    # transport：每個送往子行程的請求（回應結束前）各 attach／detach 一次
    def attach(self) -> None:
        if not self._closed:
            self._refs += 1

    def detach(self) -> None:
        if self._refs > 0:
            self._refs -= 1
        self._maybe_close()

    def finish(self) -> None:
        """proxy 處理完這個請求（回應已交給 ASGI、或已丟出例外）。"""
        self._finished = True
        self._maybe_close()

    def _maybe_close(self) -> None:
        if self._finished and self._refs == 0 and not self._closed:
            self._closed = True
            self._refund_all()
