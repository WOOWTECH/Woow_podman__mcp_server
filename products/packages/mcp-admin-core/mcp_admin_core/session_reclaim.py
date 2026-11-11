"""閘道主動回收上游閒置的 MCP session（名額已滿時）。

背景（n8n，2026-10-26 第二輪驗證）：上游 n8n-mcp（``dist/http-server-single-session.js``）的 Streamable HTTP session
有名額上限（``N8N_MCP_MAX_SESSIONS``），名額滿時新的 ``initialize`` 回 HTTP 429＋JSON-RPC -32000
``Session limit reached (N). Please wait for existing sessions to expire.``。session 只在三種情況結束：用戶端送
``DELETE /mcp``（帶 ``Mcp-Session-Id``，上游立刻移除並回 204）、子行程重啟、上游每 5 分鐘一次的清掃移除閒置超過
``SESSION_TIMEOUT_MINUTES`` 的 session。Claude.ai 等用戶端重連時不送 DELETE，舊 session 一直占名額；名額用完後，
新的 ``initialize`` 會一直被拒，直到最舊的 session 逾時（改版前的預設約 1 小時）。

閘道看得到每一個 session（子行程只綁 127.0.0.1，所有請求都經過 proxy），所以由閘道回收：

- **記錄**：上游對 ``initialize`` 回 2xx 並帶 ``Mcp-Session-Id`` 時開始記錄；之後帶這個 id 的 POST／DELETE 進來
  （開始）與回應送完（結束）都更新「最後活動時間」，進行中的請求另外計數（例如還沒回完的 tools/call）。GET 的 SSE
  串流不算使用中、也不算活動（proxy 不對 GET 呼叫 :meth:`SessionReclaimer.begin`）：上游 n8n-mcp 也只在 POST 更新
  lastAccess，開著 GET 串流的 session 照樣逾時清掉；真的 Claude Code 連上後一直開著 GET 串流，算使用中的話執行中的
  Claude Code 永遠回收不到，而它被結束後下一次呼叫會自己重新 initialize。用戶端送 DELETE 或上游回 404 時移除；子行程換了一個（重啟，``generation`` 不同）就丟掉舊紀錄。只記錄上游真的建立的 id，用戶端帶來的
  陌生 id 不會進表；表的大小有上限。
- **回收**：已通過 MCP token 驗證的 ``initialize`` 拿到「名額已滿」時（判斷由產品提供），挑「沒有進行中的請求、
  閒置超過下限（:data:`IDLE_FLOOR_SECONDS`）、最久沒活動」的 session，對上游送 ``DELETE``；204 就重試
  ``initialize``，404（上游早已清掉）就換下一個。全部都在使用中時，把上游的 429 原樣回給用戶端。每次回收記一行
  log（不含任何 token；session id 只記 sha256 前 8 碼）。
- **併發**：回收＋重試在同一把鎖內，同時在等回收的 ``initialize`` 一個一個來，不會為同一個空位重複結束 session。
  沒有走回收的 ``initialize``（第一次嘗試不經過這把鎖）仍可能先用掉剛釋放的名額；這時重試又拿到「名額已滿」，
  閘道就再回收下一個閒置的 session 再重試。一個 ``initialize`` 最多送 :data:`MAX_DELETE_ATTEMPTS` 個 DELETE
  （:class:`ReclaimBudget`，含 404 換下一個），用完就回 429。
- **頻率上限**：每 :data:`RECLAIM_WINDOW_SECONDS` 秒最多結束 :data:`MAX_RECLAIMS_PER_WINDOW` 個 session（所有請求
  合計）。共用同一個 token 的用戶端如果不停重連，最多只能把這麼多個閒置的 session 踢掉，超過就回 429。
- **DELETE 沒有釋放名額**（上游回 204／404 以外，例如認證限流 429、401、500，或送不出去）：session 可能還占著名額，
  紀錄保留（之後還能回收，或等上游逾時清掉時被丟掉），只是 :data:`RETRY_COOLDOWN_SECONDS` 內不再對它送 DELETE；
  這次回收到此為止，把上游的 429 回給用戶端。只有 200／202／204（已釋放）與 404（已不存在）才移除紀錄。
- 被回收的用戶端下次帶舊 id 來會拿到上游的 404；MCP 規格要求用戶端此時重新 ``initialize``。真的 Claude Code 2.1.310
  實測（第二輪，本機真的 n8n-mcp 2.96.2）：會自己重新 initialize 並重試那次呼叫（重新連線時同時送兩個 initialize、
  一秒後再一個，多出來的 session 沒人用，之後由本模組或上游清掉）；重新 initialize 拿到 429 時那次呼叫失敗。
  Claude Code 結束時不送 DELETE。Claude.ai 還沒實測。

只有宣告了 :class:`SessionReclaimer` 的產品（``create_app(session_reclaimer=...)``）會啟用；proxy 只對 ``/mcp``
（Streamable HTTP）記錄與回收，legacy SSE（``/sse``＋``/messages``）的 session 綁在 GET 連線上、斷線就結束，不處理。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

#: 閒置多久以上的 session 才可以被回收（秒）。要比人在對話中的停頓（看結果、想下一步、打字）長，正在用的用戶端才不會
#: 被踢掉；第二輪審查認為原本的 60 秒偏短，改成 5 分鐘（上游自己的清掃週期也是 5 分鐘）。
IDLE_FLOOR_SECONDS = 300.0
#: 一個 initialize 請求最多對上游送幾個 DELETE（遇到上游早已清掉的 404 時換下一個；剛釋放的名額被別的 initialize
#: 先用掉時再回收下一個，也算在內）。
MAX_DELETE_ATTEMPTS = 5
#: 每 :data:`RECLAIM_WINDOW_SECONDS` 秒最多結束幾個 session（所有請求合計；只算真的釋放了名額的 DELETE）。
#: 比正常的重連速度高得多（20 個名額，一分鐘內有 10 個新用戶端都要名額的情況很少），不停重連的用戶端最多只能把
#: 這麼多個閒置的 session 踢掉。
MAX_RECLAIMS_PER_WINDOW = 10
RECLAIM_WINDOW_SECONDS = 60.0
#: 最多記錄幾個 session（只有上游真的建立的 id 會進表；超過時丟掉最久沒活動、沒有進行中請求的）。
MAX_TRACKED_SESSIONS = 512
#: 對上游送 DELETE 的逾時（秒）；子行程在同一個容器的 loopback 上。
DELETE_TIMEOUT_SECONDS = 10.0
#: DELETE 沒有釋放名額（上游拒絕或送不出去）的 session，多久之內不再對它送 DELETE（秒）。
RETRY_COOLDOWN_SECONDS = 60.0
#: DELETE 回這些狀態碼就代表名額已釋放（上游 n8n-mcp 回 204）。
_RELEASED = frozenset({200, 202, 204})
#: DELETE 回這個狀態碼代表上游早已沒有這個 session（逾時清掉、連線中斷時清掉）。
_GONE = 404


def session_tag(session_id: str) -> str:
    """log 用的 session 代號：sha256 前 8 碼（不把 session id 本身寫進 log）。"""
    return hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()[:8]


class ReclaimBudget:
    """一個 ``initialize`` 請求的 DELETE 額度：跨「回收 → 重試 → 名額又被搶走 → 再回收」的每一輪。"""

    def __init__(self, deletes: int) -> None:
        self.deletes = max(0, int(deletes))


@dataclass
class _Tracked:
    generation: Any
    last_activity: float
    in_flight: int = 0
    #: DELETE 沒有釋放名額之後，這個時間（clock）之前不再對它送 DELETE。
    retry_after: float = 0.0


class SessionReclaimer:
    """記錄上游 session 的活動，名額已滿時回收最久沒活動的閒置 session（見模組說明）。

    ``is_session_limit(status, body)``：上游這個回應是不是「名額已滿」（產品提供，例如 n8n 的 429＋-32000）。
    ``session_lifetime(connection)``：依 ``config.json`` 的 ``connection`` 算出「閒置多久之後上游一定已經清掉」（秒），
    回收前先丟掉這些過期紀錄，不浪費 DELETE；不提供就不做這一步。
    """

    def __init__(
        self,
        *,
        is_session_limit: Callable[[int, bytes], bool],
        session_lifetime: Callable[[Mapping[str, Any]], float | None] | None = None,
        idle_floor_seconds: float = IDLE_FLOOR_SECONDS,
        max_delete_attempts: int = MAX_DELETE_ATTEMPTS,
        max_tracked: int = MAX_TRACKED_SESSIONS,
        retry_cooldown_seconds: float = RETRY_COOLDOWN_SECONDS,
        max_reclaims_per_window: int = MAX_RECLAIMS_PER_WINDOW,
        reclaim_window_seconds: float = RECLAIM_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        label: str = "MCP",
    ) -> None:
        self.is_session_limit = is_session_limit
        self._session_lifetime = session_lifetime
        self.idle_floor_seconds = float(idle_floor_seconds)
        self.max_delete_attempts = max(1, int(max_delete_attempts))
        self.max_tracked = max(1, int(max_tracked))
        self.retry_cooldown_seconds = float(retry_cooldown_seconds)
        self.max_reclaims_per_window = max(1, int(max_reclaims_per_window))
        self.reclaim_window_seconds = float(reclaim_window_seconds)
        self._clock = clock
        self.label = label
        self._sessions: dict[str, _Tracked] = {}
        self._released_at: deque[float] = deque()  # 最近結束 session 的時間（頻率上限用）
        self._rate_logged_at: float | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def fork(self, *, label_suffix: str) -> "SessionReclaimer":
        """An independent quota and session table for another MCP child."""
        return SessionReclaimer(
            is_session_limit=self.is_session_limit,
            session_lifetime=self._session_lifetime,
            idle_floor_seconds=self.idle_floor_seconds,
            max_delete_attempts=self.max_delete_attempts,
            max_tracked=self.max_tracked,
            retry_cooldown_seconds=self.retry_cooldown_seconds,
            max_reclaims_per_window=self.max_reclaims_per_window,
            reclaim_window_seconds=self.reclaim_window_seconds,
            clock=self._clock,
            label=f"{self.label}:{label_suffix}",
        )

    # ------------------------------------------------------------------
    # 記錄
    # ------------------------------------------------------------------

    def tracked(self) -> dict[str, dict[str, Any]]:
        """目前的紀錄（測試／除錯用；不含 session id 以外的任何輸入）。"""
        now = self._clock()
        return {sid: {"generation": t.generation, "idle_seconds": now - t.last_activity, "in_flight": t.in_flight,
                      "cooldown_seconds": max(0.0, t.retry_after - now)}
                for sid, t in self._sessions.items()}

    def created(self, session_id: str | None, generation: Any) -> None:
        """上游對 initialize 回 2xx 並帶這個 session id：開始（或重新）記錄。"""
        if not session_id:
            return
        current = self._sessions.get(session_id)
        if current is not None and current.generation == generation:
            current.last_activity = self._clock()
            return
        self._sessions[session_id] = _Tracked(generation=generation, last_activity=self._clock())
        self._evict_overflow()

    def forget(self, session_id: str | None) -> None:
        """session 已經不存在（用戶端 DELETE、上游回 404、閘道回收）：移除紀錄。"""
        if session_id:
            self._sessions.pop(session_id, None)

    def begin(self, session_id: str | None, generation: Any) -> Callable[[], None]:
        """帶 session id 的請求開始：更新最後活動時間、進行中＋1。

        回傳「請求結束」的 callback（回應送完、失敗或用戶端斷線時呼叫；只會生效一次）。不認得的 id 不記錄。
        """
        tracked = self._sessions.get(session_id) if session_id else None
        if tracked is None or tracked.generation != generation:
            return _noop
        tracked.in_flight += 1
        tracked.last_activity = self._clock()
        done = False

        def finish() -> None:
            nonlocal done
            if done:
                return
            done = True
            tracked.in_flight = max(0, tracked.in_flight - 1)
            tracked.last_activity = self._clock()

        return finish

    def _evict_overflow(self) -> None:
        if len(self._sessions) <= self.max_tracked:
            return
        idle = sorted((t.last_activity, sid) for sid, t in self._sessions.items() if t.in_flight == 0)
        for _, sid in idle[: len(self._sessions) - self.max_tracked]:
            del self._sessions[sid]

    def idle_candidates(self, generation: Any, lifetime: float | None = None) -> list[tuple[str, float]]:
        """可以回收的 session（最久沒活動的在前）：同一個子行程、沒有進行中的請求、閒置超過下限、不在冷卻中。

        閒置超過 *lifetime* 的紀錄上游一定已經清掉，順手丟掉；別的子行程留下的紀錄也丟掉。
        """
        now = self._clock()
        candidates = []
        for sid, tracked in list(self._sessions.items()):
            idle = now - tracked.last_activity
            if tracked.generation != generation or (
                    lifetime is not None and tracked.in_flight == 0 and idle > lifetime):
                del self._sessions[sid]
                continue
            if tracked.in_flight == 0 and idle >= self.idle_floor_seconds and tracked.retry_after <= now:
                candidates.append((sid, idle))
        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates

    def lifetime(self, connection: Mapping[str, Any] | None) -> float | None:
        if self._session_lifetime is None:
            return None
        try:
            return self._session_lifetime(connection if isinstance(connection, Mapping) else {})
        except Exception:  # 設定讀不懂就不清，只是多送幾個 DELETE
            return None

    # ------------------------------------------------------------------
    # 回收
    # ------------------------------------------------------------------

    def serialized(self) -> asyncio.Lock:
        """回收＋重試用的鎖（每個事件迴圈一把；測試會在不同的事件迴圈裡用同一個 app）。"""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def budget(self) -> ReclaimBudget:
        """一個 ``initialize`` 請求的 DELETE 額度（:data:`MAX_DELETE_ATTEMPTS`）。"""
        return ReclaimBudget(self.max_delete_attempts)

    def _rate_limited(self) -> bool:
        """最近 :attr:`reclaim_window_seconds` 秒內結束的 session 是否已達上限。"""
        now = self._clock()
        while self._released_at and now - self._released_at[0] >= self.reclaim_window_seconds:
            self._released_at.popleft()
        if len(self._released_at) < self.max_reclaims_per_window:
            return False
        if self._rate_logged_at is None or now - self._rate_logged_at >= self.reclaim_window_seconds:
            self._rate_logged_at = now  # 一個時間窗只記一行，不讓不停重連的用戶端把 log 洗掉
            logger.warning("%s session 名額已滿：最近 %d 秒已結束 %d 個 session（上限），這段時間不再回收、回 429",
                           self.label, int(self.reclaim_window_seconds), len(self._released_at))
        return True

    async def reclaim(self, delete: Callable[[str], Awaitable[int]], generation: Any,
                      lifetime: float | None = None, budget: ReclaimBudget | None = None) -> bool:
        """對最久沒活動的閒置 session 送 DELETE，直到有一個真的釋放了名額；回傳是否釋放。

        *delete(session_id)* 回上游的 HTTP 狀態碼。呼叫端負責：只在已通過 MCP token 驗證、上游回「名額已滿」時呼叫，
        並持有 :meth:`serialized` 的鎖直到重試完成。*budget*：這個 ``initialize`` 剩下的 DELETE 額度（不給就是新的
        :meth:`budget`）；重試又拿到「名額已滿」時，呼叫端用同一份額度再呼叫一次。
        """
        budget = budget if budget is not None else self.budget()
        candidates = self.idle_candidates(generation, lifetime)
        if not candidates:
            logger.info("%s session 名額已滿：沒有閒置超過 %d 秒的 session 可回收，回 429",
                        self.label, int(self.idle_floor_seconds))
            return False
        for attempt, (sid, idle) in enumerate(candidates, start=1):
            if budget.deletes <= 0:
                logger.info("%s session 名額已滿：這個 initialize 已送了 %d 個 DELETE，不再回收，回 429",
                            self.label, self.max_delete_attempts)
                return False
            if self._rate_limited():
                return False
            tracked = self._sessions.get(sid)
            if tracked is None or tracked.in_flight or tracked.retry_after > self._clock():
                continue  # 等 DELETE 的期間又有人用了，或剛被冷卻
            budget.deletes -= 1
            try:
                status = await delete(sid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 送不出去或逾時：上游可能還留著這個 session，紀錄保留、冷卻後才再試。
                self._cool_down(sid)
                logger.warning("%s session 名額已滿：回收 session %s 失敗（%s），保留紀錄、%d 秒內不再對它回收",
                               self.label, session_tag(sid), type(exc).__name__, int(self.retry_cooldown_seconds))
                return False
            if status in _RELEASED:
                self.forget(sid)
                self._released_at.append(self._clock())
                logger.warning(
                    "%s session 名額已滿：閘道結束了最久沒活動的 session %s（閒置 %d 秒，DELETE %s；第 %d 個候選，"
                    "共 %d 個閒置），重試 initialize", self.label, session_tag(sid), int(idle), status, attempt,
                    len(candidates))
                return True
            if status != _GONE:
                # 例如上游的認證限流（429，所有請求都從 127.0.0.1 進來、共用額度）、401、500：session 還在、還占名額。
                self._cool_down(sid)
                logger.warning("%s session 名額已滿：上游拒絕結束 session %s（DELETE %s），保留紀錄、%d 秒內不再對它"
                               "回收", self.label, session_tag(sid), status, int(self.retry_cooldown_seconds))
                return False
            self.forget(sid)
            logger.info("%s session %s 上游已不存在（DELETE 404），換下一個", self.label, session_tag(sid))
        return False

    def _cool_down(self, session_id: str) -> None:
        tracked = self._sessions.get(session_id)
        if tracked is not None:
            tracked.retry_after = self._clock() + self.retry_cooldown_seconds


def _noop() -> None:
    return None
