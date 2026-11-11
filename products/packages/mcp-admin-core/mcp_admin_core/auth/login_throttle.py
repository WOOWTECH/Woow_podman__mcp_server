"""管理登入的暴力破解限流（AI-FIX-SEC-1／AI-HER-008-4）。

同一個用戶端 IP 在 ``window_seconds`` 內連續輸錯 ``max_failures`` 次密碼，就鎖
``lock_seconds`` 秒：鎖定期間任何登入請求（包含正確密碼）都回 429＋Retry-After。
鎖定以**用戶端 IP**為鍵、不是帳號——陌生人無法藉由亂試把真正的管理員鎖在外面。
正確登入會清掉該 IP 的失敗計數。

用戶端 IP 取 ``CF-Connecting-IP``（Cloudflare 邊緣會覆寫，偽造值到不了這裡）；
不是合法 IP 或沒有這個標頭（叢集內直連、本機）就用 TCP 對端位址。
``X-Forwarded-For`` 一律不看。

狀態只存在記憶體（單一 process；重啟即清空——也是營運側解鎖的方式）。表大小有上限，
滿了先丟過期的、再丟最舊的，所以灌假 IP 也撐不爆記憶體。

環境變數（0 或負數的 ``MCP_ADMIN_LOGIN_MAX_FAILURES`` 代表停用限流）：
``MCP_ADMIN_LOGIN_MAX_FAILURES``（10）、``MCP_ADMIN_LOGIN_WINDOW_SECONDS``（120）、
``MCP_ADMIN_LOGIN_LOCK_SECONDS``（300）。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from typing import Callable

from starlette.requests import Request

logger = logging.getLogger(__name__)

_MAX_TRACKED = 4096


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


def client_key(request: Request) -> str:
    """限流用的用戶端位址：合法的 ``CF-Connecting-IP``，否則 TCP 對端。"""
    header = request.headers.get("cf-connecting-ip", "").strip()
    if header:
        try:
            return str(ipaddress.ip_address(header))
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


class LoginThrottle:
    """固定視窗失敗計數＋鎖定。執行緒安全；``clock`` 可注入（測試用）。"""

    def __init__(
        self,
        max_failures: int = 10,
        window_seconds: int = 120,
        lock_seconds: int = 300,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window = float(window_seconds)
        self.lock_seconds = int(lock_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        # key -> [failures, window_start, locked_until]
        self._state: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls) -> "LoginThrottle":
        return cls(
            _env_int("MCP_ADMIN_LOGIN_MAX_FAILURES", 10),
            _env_int("MCP_ADMIN_LOGIN_WINDOW_SECONDS", 120),
            _env_int("MCP_ADMIN_LOGIN_LOCK_SECONDS", 300),
        )

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def retry_after(self, key: str) -> int:
        """鎖定中回剩餘秒數（>=1），否則 0（只看、不計數）。"""
        if not self.enabled:
            return 0
        now = self._clock()
        with self._lock:
            return self._remaining(self._state.get(key), now)

    def begin(self, key: str) -> int:
        """一次登入嘗試開始時呼叫。

        鎖定中回剩餘秒數（呼叫端直接回 429、不驗證密碼）；否則先把這次**預記成一次失敗**
        並回 0——先記再驗，平行猛打的請求不會在第一個失敗寫入前全部溜過去。第 ``max_failures``
        次預記會立即上鎖（這一次本身仍會被驗證，輸對了 ``succeed`` 會清掉）。
        """
        if not self.enabled:
            return 0
        now = self._clock()
        with self._lock:
            st = self._state.get(key)
            left = self._remaining(st, now)
            if left:
                return left
            if st is None:
                self._make_room(now)
                st = self._state[key] = [0.0, now, 0.0]
            if now - st[1] > self.window:
                st[0], st[1] = 0.0, now  # the previous window is over
            st[0] += 1
            if st[0] >= self.max_failures:
                st[0], st[1], st[2] = 0.0, now, now + self.lock_seconds
                logger.warning(
                    "login throttle: %s locked for %d s after %d failed logins",
                    key, self.lock_seconds, self.max_failures,
                )
            return 0

    def succeed(self, key: str) -> None:
        """密碼正確：忘掉這個 IP 之前的輸錯（含剛被預記上的鎖）。"""
        with self._lock:
            self._state.pop(key, None)

    @staticmethod
    def _remaining(st: list[float] | None, now: float) -> int:
        if st is None or st[2] <= now:
            return 0
        return max(1, int(st[2] - now + 0.999))

    def _make_room(self, now: float) -> None:
        if len(self._state) < _MAX_TRACKED:
            return
        for k in [k for k, st in self._state.items() if st[2] <= now and now - st[1] > self.window]:
            del self._state[k]
        while len(self._state) >= _MAX_TRACKED:
            # 最舊的視窗先丟（dict 保持插入順序）
            del self._state[next(iter(self._state))]
