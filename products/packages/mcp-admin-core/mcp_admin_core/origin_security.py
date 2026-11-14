"""Deployment-owned Origin policy for the authenticated MCP HTTP surface.

``/private_<token>`` 的請求依 ``MCP_ADMIN_ALLOWED_ORIGINS`` 判斷，``MCP_ADMIN_ORIGIN_MODE`` 決定沒通過時怎麼辦
（負責人 2026-11-12：上線時先不擋任何 Origin，看過紀錄再逐個服務改成 enforce）：

- ``report-only``（預設；未設定或空字串也是）：照樣逐一判斷，但**不擋任何請求**。沒通過的請求記一行
  ``decision=would_deny``，再整個交給與 #136 之前正式站相同設定的 Starlette ``CORSMiddleware``（:data:`LEGACY_CORS`，
  包同一個下游 app）：preflight 與實際請求的狀態碼、CORS 標頭與內容都與當時相同。
- ``enforce``：沒通過就 403（``ORIGIN_DENIED``；清單內來源的 preflight 要了清單外的方法或標頭是
  ``CORS_PREFLIGHT_DENIED``），不帶 CORS 標頭，記一行 ``decision=deny``。

兩種模式都一樣的：沒有 Origin 的請求原樣放行（原生 MCP 客戶端仍要 URL token）；清單內的來源得到精確的 CORS 標頭與
preflight 回答。「沒通過」＝Origin 不在清單、畸形、``null``、重複，或清單內來源的 preflight 要了 GET／POST／DELETE 以外的
方法或 MCP 用不到的標頭——也就是 enforce 會回 403 的全部情況，所以 report-only 的每一行 would_deny 都對應 enforce 的一次
403。Headscale（``HEADSCALE_MCP_ALLOWED_ORIGINS``）一律 ``enforce``、不看 ``MCP_ADMIN_ORIGIN_MODE``：它在 #136 之前
就擋不受信任的 Origin，這次不能放寬。清單或模式不合法時服務啟動失敗（:func:`check_origin_policy_at_startup`）。

沒通過的來源記一行 WARNING（:func:`log_denial`），讓維運看得出真實用戶端送了哪個 Origin：只記判定、模式、原因代碼、
正規化後的 ``scheme://host[:port]``（解析不了就記跳脫過、最多 120 字的可印 ASCII）、preflight 被拒時清單外的方法或
標頭名稱，與生效中的環境變數名稱；**不記請求路徑（含 URL token）、query、其他標頭的值、cookie 或內容**。每個來源每 60 秒
最多一行、全部每 60 秒最多 20 行，超過的只計數，之後以一行「已抑制 N 行」補上；狀態有上限（記憶體有界）。紀錄不改變回應。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

from starlette.datastructures import Headers, MutableHeaders
from starlette._utils import get_route_path
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

from .owner_defaults import _StdoutHandler

logger = logging.getLogger(__name__)
# admin 沒有設定 root logger：與 owner_defaults 相同，自己掛 stdout handler（kubectl logs 看得到），紀錄照常往上傳到
# mcp_admin_core 的 Logs 頁 ring buffer handler。啟動時那一行生效政策是 INFO，root 預設 WARNING 也要印。
if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
    logger.addHandler(_StdoutHandler())
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

ENV = "MCP_ADMIN_ALLOWED_ORIGINS"
HEADSCALE_ENV = "HEADSCALE_MCP_ALLOWED_ORIGINS"
MODE_ENV = "MCP_ADMIN_ORIGIN_MODE"
MODE_REPORT_ONLY = "report-only"
MODE_ENFORCE = "enforce"
#: 允許的模式值：全小寫、完全相同（與 chart 0.1.10 的 render 檢查一致）；未設定或空字串＝report-only。
MODES = (MODE_REPORT_ONLY, MODE_ENFORCE)
#: :class:`OriginPolicy` 的 ``mode_source``：沒設（或空字串）用預設／``MCP_ADMIN_ORIGIN_MODE`` 明確設定／Headscale 固定 enforce。
MODE_SOURCE_DEFAULT = "default"
MODE_SOURCE_HEADSCALE = "headscale"

#: #136 之前正式站（8297aa4、0e432c3 建的映像）整個 app 的 CORS 設定（Headscale 以外）。report-only 下沒通過的 MCP 請求
#: 原樣交給這個設定的 CORSMiddleware，回應就與當時相同。
LEGACY_CORS: dict = {"allow_origins": ["*"], "allow_credentials": True, "allow_methods": ["*"], "allow_headers": ["*"]}
#: scope 標記：這個請求已由 :class:`McpOriginMiddleware` 交給舊設定的 CORS，內層 app 全域的 :class:`AppCORSMiddleware`
#: 就不要再套一次（ASGI scope 只有伺服器與 middleware 寫得到，用戶端送不進來）。
LEGACY_CORS_SCOPE_KEY = "mcp_admin_core.origin_security.legacy_cors"

DECISION_DENY = "deny"
DECISION_WOULD_DENY = "would_deny"

#: ``re.ASCII``：沒有它，``re.I`` 會做 Unicode 大小寫折疊（Kelvin sign U+212A 會被當成 ``k``、U+017F 當成 ``s``），
#: 讓非 ASCII 的字元被正規化成 ASCII 後與清單比對相等。Origin 標頭本來就只該是 ASCII。
_ORIGIN = re.compile(r"(https?)://(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::([0-9]+))?", re.I | re.ASCII)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I | re.ASCII)
#: 畸形 Origin 中「看起來是 URL」的形狀（可印 ASCII、無空白）：這種值只記 ``scheme://host[:port]`` 加旗標，其餘只記摘要。
_URL_SHAPE = re.compile(r"https?://[!-~]+", re.I | re.ASCII)
#: 摘要裡可以原樣出現的 scheme 前綴（固定清單：不會把使用者的任意文字帶進 log）。
_KNOWN_SCHEMES = frozenset({"http", "https", "ftp", "ws", "wss", "file", "data", "javascript", "blob", "about",
                            "chrome-extension", "moz-extension", "capacitor", "ionic", "app"})
_SCHEME_PREFIX = re.compile(r"([a-z][a-z0-9+.-]{0,20}):", re.I | re.ASCII)
_METHODS = frozenset({"GET", "POST", "DELETE"})
_HEADERS = frozenset({"authorization", "content-type", "mcp-protocol-version", "mcp-session-id", "last-event-id"})


class OriginPolicyError(RuntimeError):
    """來源清單或模式不合法。訊息與 :attr:`problem` 都不含設定值（清單可能是貼錯、帶 token 的 MCP 網址）。"""

    def __init__(self, message: str, problem: str, env: str | None = None) -> None:
        super().__init__(message)
        #: 給啟動失敗那一行 log 的中文說明。
        self.problem = problem
        #: 設定錯的環境變數名稱（不是值）。
        self.env = env


def normalize_origin(value: str, *, headscale_legacy: bool = False) -> tuple[str, str, int]:
    match = _ORIGIN.fullmatch(value)
    if match is None:
        raise ValueError("Invalid Origin")
    scheme, host, port = match.groups()
    scheme, host = scheme.lower(), host.lower()
    if host.startswith("["):
        host = str(ipaddress.IPv6Address(host[1:-1]))
    # Headscale's published parser validated after rstrip, but preserved the
    # dots for comparison. Keep that exact legacy behaviour for Headscale only.
    elif len(host) > 253 or any(not _LABEL.fullmatch(label) for label in
                                (host.rstrip(".") if headscale_legacy else host).split(".")):
        raise ValueError("Invalid Origin hostname")
    number = int(port) if port else (443 if scheme == "https" else 80)
    if not 1 <= number <= 65535:
        raise ValueError("Invalid Origin port")
    return scheme, host, number


@lru_cache(maxsize=16)
def _parse_allowed(value: str, headscale_legacy: bool = False) -> frozenset[tuple[str, str, int]]:
    if not value.strip():
        return frozenset()
    try:
        return frozenset(normalize_origin(item.strip(), headscale_legacy=headscale_legacy)
                         for item in value.split(","))
    except ValueError:
        raise OriginPolicyError("Invalid MCP Origin allowlist",
                                "不是逗號分隔的精確 http(s) 來源清單（例如 https://claude.ai；不接受 *、null、"
                                "路徑、尾端斜線、空項目）") from None


def origin_mode(env: str = ENV) -> str:
    """生效中的模式。Headscale 一律 ``enforce``（不看 ``MCP_ADMIN_ORIGIN_MODE``）；其他值不合法 → :class:`OriginPolicyError`。"""
    return _resolve_mode(env)[0]


def _resolve_mode(env: str) -> tuple[str, str]:
    if env == HEADSCALE_ENV:
        return MODE_ENFORCE, MODE_SOURCE_HEADSCALE
    value = os.environ.get(MODE_ENV, "")
    if value == "":
        return MODE_REPORT_ONLY, MODE_SOURCE_DEFAULT
    if value in MODES:
        return value, MODE_ENV
    # 與 chart 相同：說得出原因，但不回顯設定值。
    near = value.strip().lower() in MODES
    raise OriginPolicyError(
        f"Invalid {MODE_ENV}: must be exactly 'report-only' or 'enforce' (unset or empty means report-only); "
        "value not echoed",
        ("與允許值只差大小寫或前後空白（值必須完全相同、全小寫）" if near else "不是允許的值")
        + "；只能是 report-only（預設，未設定或空字串也是）或 enforce", MODE_ENV)


@dataclass(frozen=True)
class OriginPolicy:
    """生效中的政策：來源清單的環境變數名稱、模式、模式從哪裡來、清單有幾個（正規化後不重複的）來源。"""

    env: str
    mode: str
    mode_source: str
    allowlist_entries: int


def validate_origin_policy(env: str = ENV) -> OriginPolicy:
    """Fail startup before a bad deployment can report a healthy MCP service."""
    try:
        allowed = _parse_allowed(os.environ.get(env, ""), env == HEADSCALE_ENV)
    except OriginPolicyError as exc:
        raise OriginPolicyError(str(exc), exc.problem, env) from None
    mode, source = _resolve_mode(env)
    return OriginPolicy(env=env, mode=mode, mode_source=source, allowlist_entries=len(allowed))


def check_origin_policy_at_startup(env: str = ENV) -> OriginPolicy:
    """core lifespan 在讀寫設定、啟動子行程之前呼叫：記一行生效中的政策（INFO）。

    清單或模式不合法 → 先記一行 ERROR（說明原因、不含設定值）再拋 :class:`OriginPolicyError`。這裡是在 Starlette router 的
    lifespan 裡拋出的，Starlette 會先送 ``lifespan.startup.failed`` 再往上拋，所以 uvicorn（預設 ``--lifespan auto``）記下
    原因、``Application startup failed. Exiting.``、在聽 port 之前以 exit 3 結束——不會被當成「不支援 lifespan」而照常服務
    （``tests/test_origin_report_only.py`` 以真的 uvicorn 子行程驗證）。
    """
    try:
        policy = validate_origin_policy(env)
    except OriginPolicyError as exc:
        logger.error("MCP Origin 政策設定錯誤，拒絕啟動 env=%s：%s（不顯示設定值）", exc.env, exc.problem)
        raise
    if policy.mode_source == MODE_SOURCE_HEADSCALE:
        effect = f"Headscale 一律 enforce、不看 {MODE_ENV}；Origin 沒通過的 MCP 請求回 403"
    elif policy.mode == MODE_REPORT_ONLY:
        effect = "Origin 沒通過的 MCP 請求不擋：記一行 decision=would_deny，照 #136 之前的 CORS 回應"
    else:
        effect = "Origin 沒通過的 MCP 請求回 403：記一行 decision=deny"
    logger.info("MCP Origin 政策 mode=%s mode_source=%s allowlist_entries=%d env=%s（%s）",
                policy.mode, policy.mode_source, policy.allowlist_entries, policy.env, effect)
    return policy


# ---- 被拒來源的紀錄 -------------------------------------------------------------------------------------------

#: 解析不了的 Origin 最多記這麼多字（跳脫後計算，含截斷記號）。
MAX_RENDERED_ORIGIN = 120
#: 同一個來源（原因＋顯示值）在這段時間內只記一行。
DENIAL_LOG_INTERVAL_SECONDS = 60.0
#: 每個時間窗（秒）所有來源合計最多記幾行；之後只計數。
DENIAL_LOG_WINDOW_SECONDS = 60.0
DENIAL_LOG_MAX_PER_WINDOW = 20
#: 記住「最近記過」的來源數上限（實際上受每窗上限約束，這是保險）。
DENIAL_LOG_MAX_KEYS = 256

REASON_NOT_ALLOWED = "not_allowed"
REASON_MALFORMED = "malformed"
REASON_DUPLICATE = "duplicate"
REASON_NULL = "null"
#: 來源在清單內，但 preflight 要的方法不是唯一一個 GET／POST／DELETE，或要了 MCP 用不到的標頭（enforce 回 403
#: ``CORS_PREFLIGHT_DENIED``）。這兩種的紀錄另帶 ``requested=``：要的方法，或清單外的標頭名稱（不是值）。
REASON_PREFLIGHT_METHOD = "preflight_method"
REASON_PREFLIGHT_HEADERS = "preflight_headers"


def _escape_char(char: str) -> str:
    code = ord(char)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def sanitize_origin_text(value: str, limit: int = MAX_RENDERED_ORIGIN) -> str:
    """把任意標頭值轉成可安全寫進一行 log 的可印 ASCII（不會換行、不會偽造欄位）。

    ``!``～``~`` 之間的字元原樣保留，但 ``"`` 與 ``\\`` 也跳脫（顯示值放在雙引號內，跳脫後不會提早結束）；空白、
    控制字元與非 ASCII 一律 ``\\xHH``／``\\uHHHH``。超過 *limit* 字時截斷並以 ``...`` 結尾（不切斷跳脫序列）。
    只看前面足夠的字元，長標頭不會多花時間。
    """
    pieces: list[str] = []
    size = 0
    for char in value:
        piece = char if ("!" <= char <= "~" and char not in '"\\') else _escape_char(char)
        pieces.append(piece)
        size += len(piece)
        if size > limit:
            break
    if size <= limit:
        return "".join(pieces)
    kept: list[str] = []
    size = 0
    for piece in pieces:
        if size + len(piece) > limit - 3:
            break
        kept.append(piece)
        size += len(piece)
    return "".join(kept) + "..."


def _format_origin(scheme: str, host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{shown}" if port == default else f"{scheme}://{shown}:{port}"


def _summarize_malformed(value: str) -> str:
    """非 URL 形狀的畸形 Origin 只記摘要：長度、字元類別、（固定清單內的）scheme 前綴，不含任何原文。

    ``private_<token>``、``Bearer <token>`` 這類裸 token 沒有 URL 結構，沒有 URL 結構可解析，所以一個字都不記。
    """
    classes = []
    for name, test in (("alpha", lambda c: c.isascii() and c.isalpha()), ("digit", lambda c: c.isascii() and c.isdigit()),
                       ("space", lambda c: c in " \t"), ("control", lambda c: c < " " or c == "\x7f"),
                       ("non-ascii", lambda c: not c.isascii())):
        if any(test(c) for c in value[:4096]):
            classes.append(name)
    if any("!" <= c <= "~" and not c.isalnum() for c in value[:4096]):
        classes.append("punct")
    summary = f"non-url(len={len(value)},classes={'+'.join(classes) or 'none'}"
    prefix = _SCHEME_PREFIX.match(value)
    if prefix and prefix.group(1).lower() in _KNOWN_SCHEMES:
        summary += f",scheme={prefix.group(1).lower()}"
    return summary + ")"


def _summarize_url_shaped(value: str, *, headscale_legacy: bool) -> str | None:
    """URL 形狀的畸形 Origin → ``scheme://host[:port]``（host 照 :func:`normalize_origin` 的 ASCII 規則），不記其他部分。

    Origin 本來就不該有 userinfo、路徑、query、fragment，這幾段的內容（``us:SECRET@``、``?api_key=...``、``#...``）
    一個字都不記，只在結尾加 ``(has=userinfo+path+query+fragment)`` 旗標，說明有哪幾段。解析不出合法 host 回 None
    （呼叫端改記 non-url 摘要）。
    """
    scheme, _, rest = value.partition("://")  # 反斜線與瀏覽器一樣視同 /（authority 到此結束）
    end = next((i for i, char in enumerate(rest) if char in "/?#\\"), len(rest))
    authority, tail = rest[:end], rest[end:]
    userinfo = "@" in authority
    try:
        shown = _format_origin(*normalize_origin(f"{scheme}://{authority.rsplit('@', 1)[-1]}",
                                                 headscale_legacy=headscale_legacy))
    except ValueError:
        return None
    before_fragment = tail.partition("#")[0]
    flags = [name for name, present in (
        ("userinfo", userinfo), ("path", before_fragment[:1] in ("/", "\\")),
        ("query", "?" in before_fragment), ("fragment", "#" in tail)) if present]
    return f"{shown}(has={'+'.join(flags)})" if flags else shown


def _redact_malformed(value: str, *, headscale_legacy: bool = False) -> str:
    """URL 形狀 → :func:`_summarize_url_shaped`；其他（含解析不出 host 的）→ :func:`_summarize_malformed`。"""
    if _URL_SHAPE.fullmatch(value):
        shown = _summarize_url_shaped(value, headscale_legacy=headscale_legacy)
        if shown is not None:
            return shown
    return _summarize_malformed(value)


def _render(value: str, *, headscale_legacy: bool) -> str:
    """正規化成 ``scheme://host[:port]``；解析不了就只記 URL 形狀的 scheme／host／port（其餘只記旗標），非 URL 形狀只記摘要——呼叫端一律再過一次 :func:`sanitize_origin_text`。

    合法的 Origin 沒有路徑或 query；設定錯的用戶端若把整個 MCP 網址（``/private_<token>``、``?token=``）當成
    Origin 送來，這裡就不記路徑與 query，不依賴全域的 log record factory 有沒有裝。
    """
    try:
        return _format_origin(*normalize_origin(value, headscale_legacy=headscale_legacy))
    except ValueError:
        return _redact_malformed(value, headscale_legacy=headscale_legacy)


def classify_denial(origins: list[str], *, headscale_legacy: bool = False) -> tuple[str, str]:
    """被拒的 Origin 標頭（一或多個值）→ ``(原因代碼, 顯示值)``；顯示值一定是不含引號／空白的可印 ASCII（≤ 120 字）。

    解析用 ``re.ASCII``，非 ASCII 一律是 malformed；顯示前仍都過一次 :func:`sanitize_origin_text`（只跳脫一次）。
    """
    if len(origins) > 1:
        shown = ",".join(_render(value, headscale_legacy=headscale_legacy) for value in origins[:4])
        return REASON_DUPLICATE, sanitize_origin_text(shown + (",..." if len(origins) > 4 else ""))
    value = origins[0] if origins else ""
    if value == "null":
        return REASON_NULL, "null"
    try:
        normalized = _format_origin(*normalize_origin(value, headscale_legacy=headscale_legacy))
    except ValueError:
        return REASON_MALFORMED, sanitize_origin_text(_redact_malformed(value, headscale_legacy=headscale_legacy))
    return REASON_NOT_ALLOWED, sanitize_origin_text(normalized)


class DenialLogLimiter:
    """每個來源每 *interval* 秒一行、每 *window* 秒合計最多 *max_per_window* 行；其餘只計數。

    記錄「最近記過」的鍵只在真的記一行時新增，所以大小受每窗上限約束；另有 *max_keys* 硬上限（滿了先清過期的，
    仍滿就丟最舊的）。被抑制的行數在時間窗結束後補一行：下一次被拒時先補，或由事件迴圈的計時器在窗結束時補
    （每窗最多一個計時器、一行）。執行緒安全。
    """

    def __init__(self, *, interval: float = DENIAL_LOG_INTERVAL_SECONDS, window: float = DENIAL_LOG_WINDOW_SECONDS,
                 max_per_window: int = DENIAL_LOG_MAX_PER_WINDOW, max_keys: int = DENIAL_LOG_MAX_KEYS,
                 clock=time.monotonic) -> None:
        self.interval = interval
        self.window = window
        self.max_per_window = max_per_window
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: dict[tuple[str, ...], float] = {}
        self._window_start: float | None = None
        self._window_count = 0
        self._suppressed = 0
        self._timer_pending = False

    def _roll(self, now: float) -> int:
        """新時間窗開始時回傳上一窗被抑制的行數並歸零、清掉過期的鍵（呼叫者持有鎖）。"""
        if self._window_start is not None and now - self._window_start < self.window:
            return 0
        report, self._suppressed = self._suppressed, 0
        self._window_start, self._window_count = now, 0
        self._prune(now)
        return report

    def _prune(self, now: float) -> None:
        expired = [key for key, at in self._recent.items() if now - at >= self.interval]
        for key in expired:
            del self._recent[key]
        while len(self._recent) >= self.max_keys:
            del self._recent[next(iter(self._recent))]  # dict 依插入順序：最舊的先丟

    def admit(self, key: tuple[str, ...]) -> tuple[bool, int]:
        """回傳 ``(這次要不要記, 要補記的上一窗抑制行數)``。"""
        now = self._clock()
        with self._lock:
            report = self._roll(now)
            last = self._recent.get(key)
            if (last is not None and now - last < self.interval) or self._window_count >= self.max_per_window:
                self._suppressed += 1
                return False, report
            if len(self._recent) >= self.max_keys:
                self._prune(now)
            self._recent.pop(key, None)
            self._recent[key] = now
            self._window_count += 1
            return True, report

    def take_due_summary(self) -> int:
        """時間窗已結束才回傳（並歸零）被抑制的行數；給計時器用。還沒到就回 0，留給下一次被拒時補記。"""
        with self._lock:
            self._timer_pending = False
            now = self._clock()
            if self._suppressed and self._window_start is not None and now - self._window_start >= self.window:
                return self._roll(now)
            return 0

    def schedule_summary(self) -> None:
        """有被抑制的行、還沒排計時器時，在目前的事件迴圈排一個「窗結束時補記」的計時器（每窗最多一個）。

        沒有執行中的事件迴圈（同步呼叫）就不排，留給下一次被拒時補記。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self._lock:
            if not self._suppressed or self._timer_pending or self._window_start is None:
                return
            self._timer_pending = True
            delay = max(0.0, self.window - (self._clock() - self._window_start)) + 0.01
        loop.call_later(delay, _flush_summary, self)

    def size(self) -> int:
        with self._lock:
            return len(self._recent)


_DENIAL_LIMITER = DenialLogLimiter()


def _log_suppressed(count: int, limiter: DenialLogLimiter) -> None:
    # 中性用語：同一個限流器同時算 decision=deny 與 decision=would_deny 的行。
    logger.warning("MCP Origin 未通過紀錄已抑制 suppressed=%d（每個來源每 %g 秒一行、全部每 %g 秒最多 %d 行）",
                   count, limiter.interval, limiter.window, limiter.max_per_window)


def _flush_summary(limiter: DenialLogLimiter) -> None:
    count = limiter.take_due_summary()
    if count:
        _log_suppressed(count, limiter)


def _record(limiter: DenialLogLimiter, *, mode: str, reason: str, shown: str, requested: str | None,
            env: str) -> None:
    """依模式記一行（受限流）。*shown*／*requested* 必須已經過 :func:`sanitize_origin_text`。"""
    decision = DECISION_WOULD_DENY if mode == MODE_REPORT_ONLY else DECISION_DENY
    admitted, report = limiter.admit((decision, reason, shown, requested or ""))
    if report:
        _log_suppressed(report, limiter)
    if not admitted:
        limiter.schedule_summary()
        return
    extra = f' requested="{requested}"' if requested is not None else ""
    if decision == DECISION_WOULD_DENY:
        logger.warning('MCP Origin 未通過（僅記錄，照常放行） decision=%s mode=%s reason=%s origin="%s"%s env=%s',
                       decision, MODE_REPORT_ONLY, reason, shown, extra, env)
    else:
        code = ("CORS_PREFLIGHT_DENIED" if reason in (REASON_PREFLIGHT_METHOD, REASON_PREFLIGHT_HEADERS)
                else "ORIGIN_DENIED")
        logger.warning('MCP Origin 被拒（403 %s） decision=%s mode=%s reason=%s origin="%s"%s env=%s',
                       code, decision, MODE_ENFORCE, reason, shown, extra, env)


def log_denial(origins: list[str], *, env: str = ENV, mode: str = MODE_ENFORCE,
               limiter: DenialLogLimiter | None = None) -> None:
    """記一行 MCP Origin 沒通過（受限流）。只用 Origin 標頭值與 env 名稱：呼叫端不可傳入路徑或其他標頭。

    *mode* 是這個請求實際套用的模式：``enforce``（預設：呼叫端已經回 403，例如 Headscale 的 ``admin_boundary``）記
    ``decision=deny``；``report-only`` 記 ``decision=would_deny``（請求照常放行）。
    """
    limiter = limiter if limiter is not None else _DENIAL_LIMITER
    try:
        reason, shown = classify_denial(origins, headscale_legacy=env == HEADSCALE_ENV)
        _record(limiter, mode=mode, reason=reason, shown=shown, requested=None, env=env)
    except Exception:  # noqa: BLE001 — 紀錄失敗不可影響回應（也不記例外內容：可能含標頭值）
        pass


def log_preflight_denial(origin: str, reason: str, requested: str, *, env: str = ENV, mode: str = MODE_ENFORCE,
                         limiter: DenialLogLimiter | None = None) -> None:
    """清單內的來源、preflight 要了清單外的方法或標頭：記一行（受限流）。*requested* 只能是方法或標頭名稱。"""
    limiter = limiter if limiter is not None else _DENIAL_LIMITER
    try:
        shown = sanitize_origin_text(_render(origin, headscale_legacy=env == HEADSCALE_ENV))
        _record(limiter, mode=mode, reason=reason, shown=shown, requested=sanitize_origin_text(requested), env=env)
    except Exception:  # noqa: BLE001 — 同 log_denial
        pass


def _preflight_refusal(headers: Headers) -> tuple[str, str] | None:
    """清單內來源的 preflight 不在 MCP 範圍內 → ``(原因代碼, 要了什麼)``；在範圍內 → None。

    判斷與 #136 相同：``Access-Control-Request-Method`` 必須正好一個、是 GET／POST／DELETE；
    ``Access-Control-Request-Headers`` 的每個名稱（逗號分隔、去空白、小寫）都要在 :data:`_HEADERS` 內。
    """
    methods = headers.getlist("access-control-request-method")
    if len(methods) != 1 or methods[0] not in _METHODS:
        return REASON_PREFLIGHT_METHOD, ",".join(methods)
    requested = {v.strip().lower() for h in headers.getlist("access-control-request-headers") for v in h.split(",")}
    outside = requested - _HEADERS
    if outside:
        return REASON_PREFLIGHT_HEADERS, ",".join(sorted(outside))
    return None


def _add_vary_origin(headers: MutableHeaders) -> None:
    """確保回應的 ``Vary`` 含 ``Origin``，而且只出現一次（內層的 CORS 可能已經加過；``Vary: *`` 已涵蓋就不再加）。"""
    for value in headers.getlist("vary"):
        if any(token.strip().lower() in ("origin", "*") for token in value.split(",")):
            return
    headers.add_vary_header("Origin")


class AppCORSMiddleware(CORSMiddleware):
    """``create_app`` 整個 app 的 CORS（#136 起預設不給任何來源：管理頁同源，平台也不從瀏覽器呼叫管理 API）。

    report-only 下 :class:`McpOriginMiddleware` 把沒通過的 MCP 請求交給舊設定的 CORS（:data:`LEGACY_CORS`）時會在 scope
    放 :data:`LEGACY_CORS_SCOPE_KEY`，這一層就原樣略過：那個回應與 #136 之前一樣只經過一層 CORS（否則 Starlette 會在
    同一個回應上再補一次 ``Vary: Origin``、改變標頭順序）。其他請求照 Starlette ``CORSMiddleware`` 處理，沒有任何改變。
    """

    async def __call__(self, scope, receive, send) -> None:
        if scope.get(LEGACY_CORS_SCOPE_KEY):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


class McpOriginMiddleware:
    """Validate before CORS and authentication; never trust request-supplied Host.

    只看 Starlette 實際會路由的路徑（含 ``root_path`` 與巢狀 ``Mount``）是不是 ``/private_`` 開頭；其他路徑原樣交給內層。
    沒通過的請求：``enforce`` 回 403；``report-only`` 記一行之後交給 :attr:`legacy_cors`——一個設定與 #136 之前正式站
    相同（:data:`LEGACY_CORS`）、包同一個下游 app 的 Starlette ``CORSMiddleware``，preflight 與實際請求都由它照舊回應。
    """

    def __init__(self, app, *, env: str = ENV):
        self.app = app
        self.env = env
        self.legacy_cors = CORSMiddleware(app, **LEGACY_CORS)

    async def _serve_legacy(self, scope, receive, send) -> None:
        # 內層 app 全域的 AppCORSMiddleware 看到這個標記就略過（見 LEGACY_CORS_SCOPE_KEY）。
        scope[LEGACY_CORS_SCOPE_KEY] = True
        await self.legacy_cors(scope, receive, send)

    async def __call__(self, scope, receive, send):
        # Match exactly the path Starlette will route, including root_path and
        # nested Mount prefixes. scope['path'] itself can retain those prefixes.
        if scope["type"] != "http" or not get_route_path(scope).startswith("/private_"):
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        try:
            allowed = _parse_allowed(os.environ.get(self.env, ""), self.env == HEADSCALE_ENV)
            mode = origin_mode(self.env)
        except RuntimeError:
            return await JSONResponse({"detail": "ORIGIN_POLICY_UNAVAILABLE"}, status_code=503)(scope, receive, send)
        origins = headers.getlist("origin")
        if not origins:
            return await self.app(scope, receive, send)
        try:
            permitted = len(origins) == 1 and normalize_origin(
                origins[0], headscale_legacy=self.env == HEADSCALE_ENV) in allowed
        except ValueError:
            permitted = False
        if not permitted:
            # 只交出 Origin 標頭值與 env 名稱：路徑（含 URL token）、query、其他標頭與內容一律不進 log。
            log_denial(origins, env=self.env, mode=mode)
            if mode == MODE_REPORT_ONLY:
                return await self._serve_legacy(scope, receive, send)
            return await JSONResponse({"detail": "ORIGIN_DENIED"}, status_code=403)(scope, receive, send)

        origin = origins[0]
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            refusal = _preflight_refusal(headers)
            if refusal is not None:
                log_preflight_denial(origin, *refusal, env=self.env, mode=mode)
                if mode == MODE_REPORT_ONLY:
                    return await self._serve_legacy(scope, receive, send)
                return await JSONResponse({"detail": "CORS_PREFLIGHT_DENIED"}, status_code=403)(scope, receive, send)
            response = Response(status_code=200, headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "GET, POST, DELETE",
                "Access-Control-Allow-Headers": ", ".join(sorted(_HEADERS)),
                "Vary": "Origin",
            })
            return await response(scope, receive, send)

        async def cors_send(message):
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out["Access-Control-Allow-Origin"] = origin
                out["Access-Control-Allow-Credentials"] = "true"
                out["Access-Control-Expose-Headers"] = "Mcp-Session-Id"
                _add_vary_origin(out)
            await send(message)
        return await self.app(scope, receive, cors_send)
