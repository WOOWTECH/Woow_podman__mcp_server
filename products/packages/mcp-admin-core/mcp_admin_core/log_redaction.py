"""admin 行程的 log 衛生：URL 裡的憑證不進 log（woow_paas_platform#1614）。

兩種憑證會跟著請求路徑一起被印出來：

1. **MCP path token**：MCP client 連 ``/private_{token}/mcp``，token 等於整個 MCP 端點的憑證。
2. **管理頁 JWT**：Logs 頁用 EventSource 連 ``/api/logs/stream?token=<JWT>``（EventSource 不能帶 header，
   見 ``auth/middleware.py``），JWT 等於管理頁登入。

uvicorn access log 會印完整請求路徑（含 query string）；``routers/logs.py`` 又把 ``uvicorn.access``、
``httpx``、``mcp_admin_core`` 收進 Logs 頁 ring buffer，容器 stdout（``kubectl logs``）也看得到。這裡在
record 建立當下就把 ``/private_<token>`` 改寫成 ``/private_***``、``?token=<值>`` 改寫成 ``?token=***``，
之後不論哪個 handler（stdout、ring buffer）拿到的都是遮好的內容。

做法（兩層；同一筆 record 只實際遮一次，第二層看到已遮的 record 就跳過）：

- **record factory**（主要）：包一層 ``logging`` 的 record factory，本行程**任何 logger** 產生的 record
  都先遮——含 uvicorn.error 的例外 traceback、McpProcessManager 轉錄的子行程輸出 ``[mcp-server] …``、
  各 app 自己的 logger。只改字串型的 msg／args 元素（args 維持原本的 tuple 長度，uvicorn 的
  ``AccessFormatter`` 靠它拆出 ``client_addr, method, full_path, …``）。
- **logger filter**（第二道）：在 ``uvicorn.access``／``uvicorn.error``／``httpx`` 上掛
  :class:`PathTokenRedactionFilter`，萬一之後有人換掉 record factory 也還擋得住最主要的來源。

``create_app()`` 會呼叫 :func:`install`，所以每個 app 都自動套用；各 app 不需要（也不該）自己再寫一份。

只處理本行程內的 log；PaaS 前面的 ingress／tunnel 若記錄完整 URL，仍要視為持有憑證。
"""
from __future__ import annotations

import codecs
import logging
import re
import threading
import weakref
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote

MASK = "***"
AMBIGUOUS = "[redacted: ambiguous URL encoding]"

# 複雜度與長度上限（縱深防禦）：redact() 在 scrub_record 內由 uvicorn.access 於事件迴圈執行緒同步呼叫，
# 未認證的請求就能送進任意長的路徑，所以每個步驟都必須是線性（或常數上界乘線性）。
#
# - MAX_REDACT_CHARS：單一字串最多處理這麼多字元（預設 128 KiB）。超過時只保留遮好的前段，其餘整段丟棄並附上
#   ``…[truncated N chars]``。丟棄尾端不會洩漏：憑證一定接在它的標記（``/private_``、``token=``）之後，
#   前段內被切到一半的 token 仍由前綴規則遮住；切在標記中間則 token 本體根本還沒出現。
#   截斷點優先退到最後一個空白／換行（TRUNCATE_WINDOW 內），避免把一般的長 traceback 切成「半個 URL」而被判為
#   無法判斷；真的沒有空白的超長單一 URL 才會切在字元中間，並去掉結尾不完整的 ``%x``。
# - MAX_DECODE_ROUNDS：多輪 unquote 的上限，之後仍會變就 fail-closed。
MAX_REDACT_CHARS = 131_072
TRUNCATE_WINDOW = 4096
# 截斷後加上標記的輸出仍不超過 MAX_REDACT_CHARS（保留標記長度），再遮一次不會又被截一次（r2co-4）。
TRUNCATE_RESERVE = 64
# uvicorn.access 的 path 遮罩後最多保留這麼多字元，其餘換成 ``…[truncated N chars]``（G3／G4：不讓未認證請求
# 每次放大數十 KB 的 access log）。
ACCESS_PATH_MAX_CHARS = 2048
# 截斷後的標記（``…[truncated N chars]``）最長約 30 字元；path 超過「上限 + 寬限」才截斷，這樣截斷後的結果
# （上限 + 標記）不會再被截斷第二次。有些 app 的 record factory 會重建 args 並再呼叫 scrub_record，重複遮罩必須是
# 冪等的，否則計數與標記會失真（r2co-4）。
ACCESS_PATH_MARKER_SLACK = 32
# uvicorn.access 的 path 在做任何遮罩工作之前，先把原始字串限制在這個掃描窗內（F10／G5：未認證的 60KB 路徑 20 並行，
# 遮罩成本與路徑長度成正比）。窗是顯示上限的 2 倍：遮罩只會讓字串變長一點（``token=a`` → ``token=***``），窗內的
# 前 ACCESS_PATH_MAX_CHARS 字元遮罩後足以填滿顯示上限；窗外的尾段直接丟棄並計入標記，所以任何長度的 path 每筆
# 只花固定上界的遮罩成本。
ACCESS_PATH_SCAN_CHARS = 2 * ACCESS_PATH_MAX_CHARS
MAX_DECODE_ROUNDS = 16

# 有些伺服器／proxy 會忽略反斜線，過長 UTF-8（%e0%80%af）解碼後成為 U+FFFD；這兩種字元可以夾在標記字元之間而
# 仍被當成同一個路徑，所以 pattern 一律容許它們穿插在標記內（只是多遮，不會少遮）。
_NOISE = r"[\\\ufffd]*"


def _url_word(word: str) -> str:
    """Match literal or percent-encoded ASCII without decoding logged content."""
    return "".join(
        f"(?:{re.escape(char)}|%{ord(char):02x}|%{ord(char.upper()):02x}){_NOISE}" for char in word
    )


def _spelled_word(word: str) -> str:
    """與 :func:`_url_word` 相同，但百分比編碼可以是任意層（``%70``、``%2570``、``%252570``…）。

    ``(?:25)*+`` 是 possessive：``private_`` 與分隔符的十六進位碼都不以 ``25`` 開頭，不需要回溯；一段 ``25`` 串只在它自己的
    ``%`` 起點被掃，成本與長度成正比。
    """
    return "".join(
        f"(?:{re.escape(char)}|%(?:25)*+(?:{ord(char):02x}|{ord(char.upper()):02x})){_NOISE}" for char in word
    )


# URL consumers decode percent escapes before routing/query parsing. Logs can
# preserve their original spelling, so recognise both forms before formatting.
# 所有 pattern 都以固定字面前綴開頭、中間沒有巢狀量詞，單次掃描為線性。
# 反斜線當分隔符時用 lookbehind 擋掉「從噪音連續段中間起算」的起點，否則 ``\\\\\\…`` 長串會從每個位置各掃一次。
# 前綴在解碼前（紀錄原本的拼法）與解碼後共用；任意層編碼的寫法解碼後一定也認得，所以對解碼後的字串沒有差別。
_PRIVATE_PREFIX = r"(?:/|%(?:25)*+(?:2f|5c)|(?<![\\\ufffd])\\)" + _NOISE + _spelled_word("private_")
# 解碼後先套用的區段規則（與 e0a80d6 相同）：token 到下一個 / 、空白、? 、# 或引號為止。
_PATH_TOKEN_RE = re.compile("(" + _PRIVATE_PREFIX + r")[^/\s?#\"']++", re.IGNORECASE)
# N7（2026-11-13）：token 區段。開頭先跳過 / ? # " '（``/private_//<token>``；``/private_%2F<token>`` 在 access log 是
# ``/private_/<token>``；``/private_?<token>``），之後到下一個 / 、空白、? 、# 或引號為止。區段非空就整段遮成 ``***``，
# 已遮的 ``***`` 再遮一次不變（冪等）。套在紀錄原本的拼法（解碼前）上時，``%20``、``%22``、``%3F``、``%C2%A0``、
# ``%E3%80%80`` 這類編碼過的分隔字元不是字面的分隔字元，不會讓區段提早結束。6dc34c8 起只在解碼後比對，區段停在解碼出來的
# 空白、引號、``?`` 上，token 外洩（見 :func:`_mask_spelled_private_paths`）。
_PATH_SEGMENT_RE = re.compile("(" + _PRIVATE_PREFIX + r")[/?#\"']*+[^/\s?#\"']++", re.IGNORECASE)
# 同上，但不跨過字面或任意層編碼的 ? & ; =，也不跳過開頭的 ?。只用來判斷「區段裡有沒有這些字元」：與上面的結果相同
# 就是沒有（兩次 ``sub`` 都是線性；用 ``search`` 找「有這些字元的區段」在找不到時會從每個起點各掃一次區段，是 O(n^2)）。
# 每個 ``%`` 都必須是完整的 ``%(25)*HH`` 單位、最後的 HH 不是 3f／26／3b／3d，其他寫法一律在這裡停下（結果不同就
# fail closed）：十六進位數字本身也可以編碼，``%%33%46``、``%3%46``、``%25%33%46``、``%2%353F`` 多輪解碼後是 ``?``、
# ``%%32%36`` 是 ``&``，只認 ``%(25)*3F`` 這種完整寫法的話這些都看不到（獨立審查 2026-11-13）。每個 ``%`` 後面都是
# ``(25)*HH`` 時，每一輪解碼只會把每個單位開頭的 ``%25`` 換成 ``%``、或把 ``%HH`` 換成那個字元（HH 不是 25，不會解出
# 新的 ``%``），不會和前後的字元拼出新的跳脫，解碼到底也就不會出現 ? & ; =。兩個 lookahead 的 ``(?:25)*+`` 都是
# possessive：``25`` 串一定吃完，最後的 HH 才不會是 25（會回溯的話 ``%25%33F`` 會被當成 HH＝25 的單位而放行）；
# 每個 ``%`` 只掃自己的 ``25`` 串，仍是線性。
_STRICT_PATH_SEGMENT_RE = re.compile(
    "(" + _PRIVATE_PREFIX + r")[/#\"']*+"
    r"(?:[^/\s?#\"'&;=%]|%(?=(?:25)*+[0-9a-fA-F]{2})(?!(?:25)*+(?:3f|26|3b|3d)))++",
    re.IGNORECASE)
# N7：``/private_`` 網址的 query 有「沒有 = 的參數」（``/private_x/mcp?<token>``）時，整段 query 遮成 ``?***``。
# 正常的 MCP 請求沒有 query；legacy SSE 的 ``?session_id=…`` 每個參數都有 =，保持原樣。只看區段已遮成 ``***`` 的網址
# （先跑上面的區段規則）。``***`` 之後的路徑與 lookahead 掃描的 query 都不跨過 ``*`` 與 ``?``：``*`` 只會出現在遮罩裡
# （access log 的 path 經 quote，字面 ``*`` 是 ``%2A``），下一個 ``/private_***`` 就是邊界，所以每個字元最多被一個起點
# 掃到，``/private_***/x?a=1`` 重複上萬次也是 O(n)；整段 query 只在真的要遮的時候被吃掉一次。
_BARE_QUERY_RE = re.compile(
    "(" + _PRIVATE_PREFIX + r"\*\*\*[^\s?#\"'*]*+\?)"
    r"(?=(?:[^=&;\s#\"'?*]*+=[^&;\s#\"'?*]*+[&;]|[&;])*+[^=&;\s#\"'?*]++(?!=))"
    r"[^\s#\"']++",
    re.IGNORECASE)
# F10：開頭的 lookahead 只檢查「分隔符、噪音之後的第一個字元是 a／t（或 ``%`` 開頭的編碼寫法）」這個必要條件，
# 讓 ``&&&…`` 這類長串在每個起點只做一次單字元檢查就失敗（實測 64KB 從 5 ms 降到 1.4 ms）；不改變 match 結果。
_QUERY_TOKEN_RE = re.compile(r"(?=[?&;]" + _NOISE + r"[aAtT%])"
                             + r"([?&;]" + _NOISE + r"(?:" + _url_word("access_") + r")?" + _url_word("token")
                             + r"=)[^&#\s\"']+", re.IGNORECASE)
# 憑證鍵名的「寬鬆」比對（取代原本「每個 [?&;] 起點先吃 128 字元再回溯找 =」的 key 字元類別，那是 O(128×n)，
# r2co-1）。被拒絕的 URL 可能在鍵名的字母之間夾標點、空白、反斜線、U+FFFD，甚至解碼後出現的 ``&``、``;``
# （``token%26=``、``token%3B=``、``token%20=``），所以不能用「key 遇到分隔符就停」——那會讓這些已有測試的寫法外洩。
# 改成只在「真的出現 token／access_token 這個字」時才動作（F10 起以單一 ``sub`` 實作，見 ``_LOOSE_KEY_RE``；
# 語意仍是下面這三點）：
#   1. 把「非英數、非 = # 引號」的連續字元當成一段段 lead，線性、每個字元只屬於一段；
#   2. lead 內有 ? & ; 才算參數鍵的開頭，從 lead 的結尾用 _KEY_TAIL.match（只在這一個位置比對，不掃描）
#      確認接著是 token／access_token（字母之間用 J* 穿插）加 ``=``；
#   3. 前一個 match 的值若吞進了下一個 lead 的前半（``token=a.-&token_=S``），lead 從前一個 match 結束處起算，
#      不會因此漏掉後面的鍵（驗證者 P2 的「新 match 吃掉起點」同類問題；改成不重疊掃描後整體不再依賴 lookbehind）。
# 起點只有 lead 的結尾，每個字元最多被一個 lead 與一次 _KEY_TAIL 比對（J* 連續段）看過，整體 O(n)，
# 與輸入裡有多少個 ``&``／``;`` 無關。涵蓋率不低於原 pattern：原 pattern 能遮的鍵，去掉非英數字元後必為
# token／accesstoken，也就是 lead（含 ? & ;）之後恰好是這個字加上穿插的標點。
# F10b：雜字元類別不受 IGNORECASE 影響。``_KEY_TAIL`` 整段在 IGNORECASE（或 scoped ``(?i:…)``）之內，原本的
# ``[^A-Za-z0-9=?#"']`` 會把「折疊成 ASCII 字母」的 U+017F（ſ→s）、U+212A（K→k）、U+0130（İ→i）、U+0131（ı→i）
# 也當成字母排除，字母之間夾這些字元的鍵名（``to\u017fken=``）ec82641 會遮、ff3dbc0 起不遮（退步）。
# 改用 ``(?-i:…)`` 讓類別精確地只排除 ASCII 英數與 ``=?#"'``，這四個字元就是普通雜字元；字母本身仍照 IGNORECASE
# 比對（K 仍可當 k、ſ 仍可當 s），所以遮罩範圍是 ff3dbc0 與 ec82641 的超集。
#
# 歧義與線性：ſ 既是雜字元又能當 access 的 s、K 既是雜字元又能當 token 的 k。若直接寫 ``s J* s J* t``（J 含 ſ），
# ſ 長串會多項式回溯。所以把「兩個 s 與其間、其前後的雜字元」（``_S_GAP``）與「k 與其前後的雜字元」（``_K_GAP``）
# 寫成互斥的幾個分支，每個分支都是確定性的 possessive 掃描（ASCII 的 s／k 不是雜字元，所以一定是字母；ſ／K 是
# 「自由」的，能補足字母數）：
#   s 區段（e 之後、t 之前）：ASCII s 有 2 個＝其餘都是雜字元；1 個＝另外有一個 ſ（在它之前或之後）；0 個＝至少兩個 ſ；
#   k 區段（o 之後、e 之前）：ASCII k 有 1 個；0 個則至少有一個 K。
# 各分支互斥、不重疊量詞，單點 match 仍是 O(長度)（最壞多掃幾個分支，常數倍）。
_JUNK = r"(?-i:[^A-Za-z0-9=?#\"'])"
_JUNK_NO_LONG_S = r"(?-i:[^A-Za-z0-9=?#\"'\u017f])"
_JUNK_NO_KELVIN = r"(?-i:[^A-Za-z0-9=?#\"'\u212a])"
# 分支內的字母與折疊字元都要關掉 IGNORECASE（否則字面 ſ 也會比對 s、k 也會比對 K，分支就不互斥了）。
_ASCII_S = r"(?-i:[sS])"
_ASCII_K = r"(?-i:[kK])"
_LONG_S = r"(?-i:\u017f)"
_KELVIN = r"(?-i:\u212a)"
_S_GAP = (
    r"(?:"
    + f"{_JUNK}*+{_ASCII_S}{_JUNK}*+{_ASCII_S}{_JUNK}*+"                                      # 兩個 ASCII s
    + f"|{_JUNK_NO_LONG_S}*+{_LONG_S}{_JUNK}*+{_ASCII_S}{_JUNK}*+"                      # ſ 在 s 之前
    + f"|{_JUNK_NO_LONG_S}*+{_ASCII_S}{_JUNK_NO_LONG_S}*+{_LONG_S}{_JUNK}*+"                               # ſ 在 s 之後（s 是第一個）
    + f"|{_JUNK_NO_LONG_S}*+{_LONG_S}{_JUNK_NO_LONG_S}*+{_LONG_S}{_JUNK}*+"       # 兩個 ſ
    + r")"
)
_K_GAP = (
    r"(?:"
    + f"{_JUNK}*+{_ASCII_K}{_JUNK}*+"                                                      # ASCII k
    + f"|{_JUNK_NO_KELVIN}*+{_KELVIN}{_JUNK}*+"                                   # 沒有 ASCII k：第一個 K 當 k
    + r")"
)


_KEY_ACCESS = f"a{_JUNK}*+c{_JUNK}*+c{_JUNK}*+e{_S_GAP}"
_KEY_TOKEN = f"t{_JUNK}*+o{_K_GAP}e{_JUNK}*+n{_JUNK}*+"
_KEY_TAIL = re.compile(r"((?:" + _KEY_ACCESS + r")?" + _KEY_TOKEN + r")=([^&#;\s\"']+)", re.IGNORECASE)
# F10：上面第 1～3 點原本在 Python 迴圈裡對每個 lead 連續段各做一次（切片、三個 ``in``、``_KEY_TAIL.match``），
# 段數由攻擊者決定（``/x?`` 接 3 萬個 ``&=`` ＝ 3 萬段，本機 25 ms、pod 內 65 ms，20 並行時 /healthz 1.3 秒）。
# 改成單一編譯好的 pattern 以 ``sub`` 在 C 層完成，語意與迴圈版逐字相同（fuzz 驗證，見 tests/test_log_redaction_segments.py）：
#   - lead 的字元類別不加 IGNORECASE（與 lead 連續段 ``[^A-Za-z0-9=#\"']+`` 相同；加了的話 ``[^A-Za-z]`` 會把 U+212A、U+017F 也排除），
#     鍵名與值放在 scoped ``(?i:…)`` 內，內容就是 ``_KEY_TAIL`` 的 pattern；
#   - lead 從該連續段「最後一個 ``[?&;]``」起算：``[?&;]`` 之後的字元類別排除 ``?&;``，所以 possessive 量詞吃到的
#     就是最後一個分隔符到連續段結尾，鍵名必須緊接在連續段結尾（與原本的 ``_KEY_TAIL.match(text, run.end())`` 相同）；
#     前一個 match 的值吞進後面的 lead（``token=a.-&token_=S``）時，``sub`` 從上一個 match 結束處繼續掃描，
#     等於原本「lead 從 last 起算」的規則，不需要 lookbehind；
#   - possessive 量詞不回溯，每個字元最多被一個 lead 與一次 ``_KEY_TAIL`` 看過，仍是 O(n)。
# 不可改成 CLAUDE.md 列為禁止的寫法（key 字元類別含 ``&;``、遇分隔符就停、可為空 lead 的單一 regex）。
# 開頭的 lookahead 同樣只檢查必要條件（lead 之後的第一個字元是 a／t），不改變 match 結果。
_LOOSE_KEY_RE = re.compile(r"(?=[?&;][^A-Za-z0-9=#\"'?&;]*+[aAtT])"
                           r"([?&;][^A-Za-z0-9=#\"'?&;]*+)(?i:" + _KEY_TAIL.pattern + r")")
_LOOSE_KEY_REPL = r"\g<1>\g<2>=" + MASK


def _mask_loose_keys(text: str) -> str:
    return _LOOSE_KEY_RE.sub(_LOOSE_KEY_REPL, text)


_VALID_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# 會帶請求路徑的 logger：uvicorn access／error（例外訊息含 path）、httpx（admin 自己的探測與 proxy 上游）。
PATH_LOGGERS = ("uvicorn.access", "uvicorn.error", "httpx")

_lock = threading.Lock()
_factory_installed = False


def _has_candidate(text: str) -> bool:
    low = text.lower()
    # 反斜線與 U+FFFD 可夾在標記內（p\rivate_、to\ufffdken=），與 _NOISE 一致，比對前先去掉（r2-sec-1）。
    if "\\" in low:
        low = low.replace("\\", "")
    if "\ufffd" in low:
        low = low.replace("\ufffd", "")
    # F10b：IGNORECASE 下 U+0131（ı）與 U+0130（İ，lower() 是 ``i`` 加組合點 U+0307）都比對 i，``prıvate_`` 也會被
    # 路徑 pattern 遮；必要條件檢查要用同一個範圍，否則這個輸入不會進到 pattern。
    if not low.isascii():
        low = low.replace("\u0131", "i").replace("i\u0307", "i")
    return "private" in low or "?" in low or "token=" in low or "%" in low


# F10：原本對 ``_RUN_RE`` 的每個 run 各跑一次 Python 迴圈（``text.find`` ＋ ``_INVALID_ESCAPE_RE.search``），run 數由
# 攻擊者決定。改成單一 pattern 的一次 ``search``（C 層）：lookbehind 讓起點只出現在 run 開頭（前一個字元是空白、引號
# 或字串開頭），``[^\s"'/]*+`` 吃到 run 內第一個 ``/``，之後 lazy 掃到第一個無效跳脫。possessive 量詞不回溯，每個 run
# 只被掃一次，O(n)；語意與迴圈版逐字相同（fuzz 驗證）。
_INVALID_ESCAPE_IN_URL_RE = re.compile(
    r"(?<![^\s\"'])[^\s\"'/]*+/(?:[^\s\"'%]++|%(?=[0-9a-fA-F]{2}))*+%(?![0-9a-fA-F]{2})")


def _has_invalid_escape_in_url(text: str) -> bool:
    """等價於 ``(?:/|https?://)[^\\s"']*%(?![0-9a-fA-F]{2})`` 的線性版本。

    原本的單一 regex 以每個「/」為起點往後掃到字串尾，遇到 ``/////…?x`` 就是 O(n^2)。這裡以空白與引號切成 run，
    每個 run 只看「第一個 /」之後有沒有無效跳脫（第一個 / 之後的任何 % 都在該起點的掃描範圍內），每個字元最多被看一次。
    """
    return _INVALID_ESCAPE_IN_URL_RE.search(text) is not None


# F10：``urllib.parse.unquote`` 對每個 ``%`` 跑一次 Python 迴圈（``unquote_to_bytes``），無效跳脫每個還拋一次
# ``KeyError``；64KB 的 ``%`` 要 66 ms，最多 16 回合。這裡的實作與 ``unquote(text, errors="replace")`` 逐字相同，
# 但整段都在 C 層：
#   1. 整串 UTF-8 編碼（含非 ASCII 字面字元）。``unquote`` 以 ASCII run 為單位各自解碼，字面的非 ASCII 字元一定是
#      完整序列、首位元組是 C2..F4（不是延續位元組），所以它一定會結束前面未完成的序列，與「各自解碼」等價；
#   2. 反斜線全部加倍（``escape_decode`` 只認反斜線）；
#   3. 只把「後面接兩個十六進位字元」的 ``%`` 換成 ``\x``（先換成 UTF-8 不可能出現的 ``\xff`` 占位字元，使用不含反斜線的
#      字面取代字串，比帶反斜線的樣板快），無效的 ``%`` 保持字面（等於 ``unquote`` 的 KeyError 分支）；
#   4. ``codecs.escape_decode`` 一次還原所有 ``\xNN``，再以 ``errors="replace"`` 解碼。
# 含 lone surrogate 的字串無法編成 UTF-8，直接走原本的 ``unquote``（行為相同，只是慢；網路輸入到不了這條路徑：
# uvicorn 的 path 先經 ``unquote(errors="replace")`` 再 ``quote``）。
_VALID_PERCENT_RE = re.compile(rb"%(?=[0-9a-fA-F]{2})")


def _unquote(text: str) -> str:
    """與 ``urllib.parse.unquote(text, errors="replace")`` 逐字相同，成本與有效跳脫的數量成正比（C 層）。"""
    if "%" not in text:
        return text
    try:
        data = text.encode("utf-8")
        if b"\\" in data:
            data = data.replace(b"\\", b"\\\\")
        data, count = _VALID_PERCENT_RE.subn(b"\xff", data)
        if not count:
            return text
        return codecs.escape_decode(data.replace(b"\xff", b"\\x"))[0].decode("utf-8", "replace")
    except (UnicodeError, ValueError):
        return unquote(text, errors="replace")


def _dangling_escape(text: str, cut: int) -> int:
    """在 cut 處切開時要往前退幾個字元，才不會留下不完整的 ``%x``。"""
    if text[cut - 1] == "%":
        return 1
    if cut >= 2 and text[cut - 2] == "%":
        return 2
    return 0


def _split_mask(text: str, cut: int) -> int:
    """在 cut 處切開時要往前退幾個字元，才不會把已遮好的 ``***`` 切成 ``*`` 或 ``**``。

    已遮的值被切成 ``/private_*`` 再做第二次遮罩會變回 ``/private_***``，結果就不同（R3-2）；退到 ``***`` 之前，
    第二次遮罩看到的是 ``/private_`` 加分隔字元，不會再改。只看緊鄰 cut 的 ``***``，常數時間。
    """
    if text[cut - 1:cut + 2] == MASK:
        return 1
    if cut >= 2 and text[cut - 2:cut + 1] == MASK:
        return 2
    return 0


def _safe_cut(text: str, cut: int) -> int:
    """cut 往前退到不留下不完整的 ``%x``、也不切開 ``***``（兩個條件互相影響，各最多重複兩次）。"""
    for _ in range(2):
        back = _dangling_escape(text, cut) if cut else 0
        back = back or (_split_mask(text, cut) if cut else 0)
        if not back:
            break
        cut -= back
    return cut


def _with_marker(head: str, dropped: int) -> str:
    """附上截斷標記。標記前一定隔一個空白：``/private_***``、``?token=***`` 這類已遮的值遇到空白就結束，
    再遮一次不會把標記吞進值裡（R3-2、R3-3：截斷必須冪等）。"""
    return f"{head} …[truncated {dropped} chars]"


def _truncate(text: str) -> tuple[str, int]:
    if len(text) <= MAX_REDACT_CHARS:
        return text, 0
    cut = MAX_REDACT_CHARS - TRUNCATE_RESERVE
    boundary = text.rfind("\n", cut - TRUNCATE_WINDOW, cut)
    if boundary < 0:
        boundary = text.rfind(" ", cut - TRUNCATE_WINDOW, cut)
    if boundary > 0:
        cut = boundary
    else:
        cut -= _dangling_escape(text, cut)
    return text[:cut], len(text) - cut


def redact(text: str) -> str:
    """把字串裡的 ``/private_<token>`` 與 ``?token=<值>`` 遮成 ``***``；沒有就原樣回傳。

    耗時與輸入長度成線性（見模組上方的上限說明）；超過 ``MAX_REDACT_CHARS`` 的字串會被截斷。
    """
    if not text:
        return text
    text, dropped = _truncate(text)
    result = _redact_bounded(text) if _has_candidate(text) else text
    limit = MAX_REDACT_CHARS - TRUNCATE_RESERVE
    if len(result) > (limit if dropped else MAX_REDACT_CHARS):
        # 遮罩把短 token 換成 ``***`` 會讓字串變長；已遮好的結果可以安全地再切，確保「結果加標記」不超過上限，
        # 重複遮罩才不會又截一次（前面已經截過的字串，標記要的空間也要留）。
        cut = _safe_cut(result, limit)
        dropped += len(result) - cut
        result = result[:cut]
    if dropped:
        return _with_marker(result, dropped)
    return result


def _decode_all(text: str) -> str | None:
    """多輪 unquote（最多 MAX_DECODE_ROUNDS 輪）；之後仍會變就回 None（呼叫端 fail closed）。"""
    # 沒有任何有效的 %XX 時 unquote 不會改變內容，直接略過（長串 ``%%%%`` 或非 ASCII 文字的常見快路徑）。
    for _ in range(MAX_DECODE_ROUNDS if _VALID_ESCAPE_RE.search(text) else 0):
        decoded = _unquote(text)
        if decoded == text:
            return text
        text = decoded
    if _VALID_ESCAPE_RE.search(text) and _unquote(text) != text:
        return None
    return text


def _decode_or_fail(text: str) -> str | None:
    """解碼後的工作副本；無法判斷（解碼不收斂、解碼出控制字元或新的 ``#``、網址裡有無效跳脫）時回 None。"""
    decoded = _decode_all(text)
    if decoded is None:
        return None
    if decoded != text and (_CONTROL_RE.search(decoded) or
                            ("#" in decoded and "#" not in text)):
        return None
    # An invalid escape in a URL may be a corrupted sensitive key/prefix.
    # Do not try to guess its meaning or expose the rest of that message.
    # 下面每個步驟前都先用 C 層的子字串檢查排除「不可能有 match」的輸入（只看必要條件，所以不會少遮；遮罩只會
    # 加入 ``***``，不會加入這些字元）：長串 ``&``、``%``、``/`` 這類沒有 token 字樣的輸入不必讓每個 regex 各掃一遍
    # （F10：把 64KB 病態輸入的每一步從 2～8 ms 降到接近 0）。
    if "%" in decoded and "/" in decoded and _has_invalid_escape_in_url(decoded):
        return None
    return decoded


def _keyed(text: str, low: str) -> bool:
    # 鍵名：token 的 k 與 ``=`` 一定存在（k 可能寫成 ``%6b``／``%4b``）。
    return "=" in text and ("k" in low or "%6b" in low or "%4b" in low)


def _mask_keys(text: str) -> str:
    return _mask_loose_keys(_QUERY_TOKEN_RE.sub(r"\1" + MASK, text))


def _has_key(text: str) -> bool:
    """有沒有任何憑證鍵名（``token=``／``access_token=`` 與各種寬鬆寫法）；單次 ``search``，線性。"""
    return _QUERY_TOKEN_RE.search(text) is not None or _LOOSE_KEY_RE.search(text) is not None


def _mask_spelled_private_paths(text: str, keys: bool) -> str | None:
    """N7：在紀錄原本的拼法上（解碼之前）把 ``/private_`` 的 token 區段遮成 ``***``；回 None 表示要 fail closed。

    uvicorn 的 access log 印 ``quote(unquote(path))``：token 前後或中間被貼進去的空白、引號、``?``、不換行空白、全形空白
    在紀錄裡是 ``%20``、``%22``、``%3F``、``%C2%A0``、``%E3%80%80``。解碼後這些都是區段的結束字元，區段提早結束或根本
    比對不到（e0a80d6 有 23 種寫法外洩）；在原本的拼法上它們只是一般字元，區段一路到下一個字面的分隔字元（8297aa4 的
    字面規則就是這樣遮住的）。區段可以先跳過開頭的 / ? # " '，``/private_?<token>``、``/private_//<token>`` 也遮。

    ``keys``＝解碼後的字串裡有憑證鍵名。這時區段裡若有字面或編碼的 ? & ; =（含十六進位數字本身也編碼、多輪解碼後才
    出現的 ``%3%46``、``%%32%36`` 這類寫法：不是完整 ``%(25)*HH`` 單位的 ``%`` 一律算），或跳過了開頭的 ``?``，這些字元
    解碼後可能是鍵名的 lead、``=`` 或值的一部分（``/private_x%3F/to/ken=<值>``、``/private_x%20&token=<值>/y``、
    ``/private_x%3%46to/ken%3D<值>``），先把區段遮掉的話解碼後的鍵名規則就看不到，舊版會遮的值反而外洩；分不出哪一段是
    token、哪一段是鍵名，所以整句 fail closed。
    沒有鍵名時沒有東西要保護，區段照遮，``/private_`` 網址的裸 query（``/private_x/mcp?<token>``）也在這裡先遮——
    解碼後 ``%20`` 會讓 query 提早結束。全部是單一 ``sub``（C 層），沒有每段一次的 Python 工作。
    """
    low = text.lower()
    if "_" not in text and "%5f" not in low and "%25" not in low:  # private_ 的底線：字面、%5f 或多層 %255f
        return text
    masked = _PATH_SEGMENT_RE.sub(r"\1" + MASK, text)
    if masked == text:
        return text
    if keys:
        return masked if _STRICT_PATH_SEGMENT_RE.sub(r"\1" + MASK, text) == masked else None
    return _mask_bare_queries(masked, keyed=False) if "?" in masked else masked


def _mask_bare_queries(text: str, keyed: bool) -> str:
    """把 ``/private_***…?<裸參數>`` 的 query 遮成 ``?***``（:data:`_BARE_QUERY_RE`），並收掉遮罩產生的連鎖：

    - 前一個網址的 query 吃進了後一個 ``/private_`` 開頭（``/private_***/private_?/`` → ``/private_***/private_?***``），
      區段規則會再把 ``?***`` 收成 ``***``；
    - ``?***`` 加上後面的空白可能接成另一個鍵名的 lead（``?*** to/ken=<值>``），再遮一次鍵名（先鍵名、後區段）。
    兩者都只在這一遍真的遮了東西時才做，之後再遮一次不會有任何改變（冪等）。
    """
    masked = _BARE_QUERY_RE.sub(r"\1" + MASK, text)
    if masked == text:
        return text
    if keyed:
        masked = _mask_keys(masked)
    return _PATH_SEGMENT_RE.sub(r"\1" + MASK, masked)


def _redact_bounded(text: str) -> str:
    # Decode a working copy, never just one layer: Uvicorn logs raw request
    # targets even when routing rejects them. Bound work and fail closed.
    decoded = _decode_or_fail(text)
    if decoded is None:
        return AMBIGUOUS
    # N7：/private_ 區段先在原本的拼法上遮。「無法判斷」的判定先對原本的字串做一次（與 e0a80d6 相同，修正不會讓任何一句
    # 不再 fail closed），區段遮過之後再對遮過的字串做一次（遮掉的區段可能剛好隔開了一段網址，例如 ``"``）。
    keys = _keyed(decoded, decoded.lower()) and _has_key(decoded)
    spelled = _mask_spelled_private_paths(text, keys)
    if spelled is None:
        return AMBIGUOUS
    if spelled != text:
        decoded = _decode_or_fail(spelled)
        if decoded is None:
            return AMBIGUOUS
    low = decoded.lower()  # U+212A（Kelvin）的 lower() 是 ASCII k，與 IGNORECASE 比對 k 的範圍一致
    keyed = _keyed(decoded, low)
    # 解碼後才看得到的前綴（``%2570rivate_``、``p%5Crivate_``…）。解碼沒有改變字串時，區段規則已經在
    # _mask_spelled_private_paths 套過同一個字串，不必再整串掃一遍。路徑前綴：``private_`` 的底線一定是字面 ``_`` 或 ``%5f``。
    fresh = decoded != spelled and ("_" in decoded or "%5f" in low)
    cleaned = decoded
    # 先照 e0a80d6 的順序（先區段、後鍵名）遮一遍，遮到的至少與舊版相同；再補 N7 的規則：區段可跳過開頭的 / ? # " '、
    # /private_ 網址的裸 query。
    if fresh:
        cleaned = _PATH_TOKEN_RE.sub(r"\1" + MASK, cleaned)
    if keyed:
        cleaned = _mask_keys(cleaned)
    if fresh:
        cleaned = _PATH_SEGMENT_RE.sub(r"\1" + MASK, cleaned)
    # 沒有鍵名、也沒有解碼後才出現的前綴時，裸 query 已經在原本的拼法上遮過。
    if (fresh or keys) and "?" in cleaned and MASK in cleaned:
        cleaned = _mask_bare_queries(cleaned, keyed)
    if cleaned == decoded:
        # Preserve normal, non-sensitive encoded URLs exactly as logged.
        return spelled
    # 跳過的引號被遮掉後，前後兩段可能接成同一段網址，原本分開的無效跳脫就落在網址裡；再遮一次會變成無法判斷，
    # 現在就判定，結果才冪等。
    if "%" in cleaned and "/" in cleaned and _has_invalid_escape_in_url(cleaned):
        return AMBIGUOUS
    return cleaned


def _redact_value(value: Any) -> Any:
    """str 直接遮；非基本型別（URL 物件、例外等）只有字串形式真的含憑證時才換成遮好的字串。"""
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (bool, int, float, bytes)):
        return value
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — 轉字串失敗就交給 logging 自己處理
        return value
    cleaned = redact(text)
    return value if cleaned == text else cleaned


def _redact_args(args: Any) -> Any:
    if isinstance(args, tuple):
        return tuple(_redact_value(a) for a in args)
    if isinstance(args, Mapping):
        return {k: _redact_value(v) for k, v in args.items()}
    return args


_UVICORN_ACCESS_TEMPLATE = '%s - "%s %s HTTP/%s" %d'
# uvicorn.access 的 client／method／http version 只會是這種短 token；不符就視為被夾帶內容。
_ACCESS_ATOM_RE = re.compile(r"[A-Za-z0-9._:\[\]-]{0,64}")


def _is_uvicorn_access(record: logging.LogRecord) -> bool:
    # Uvicorn's AccessFormatter requires this exact template and its five
    # structured arguments (client, method, path, http version, status). The
    # template itself contains no URL.
    return (record.name == "uvicorn.access" and record.msg == _UVICORN_ACCESS_TEMPLATE
            and isinstance(record.args, tuple) and len(record.args) == 5)


def _render(record: logging.LogRecord) -> str | None:
    try:
        return record.getMessage()
    except Exception:  # noqa: BLE001 — malformed logging must not print its arguments
        return None


def _scrub_access_path(path: Any) -> Any:
    """uvicorn.access 的 path：先限制掃描窗，再遮罩，最後做遮罩後截斷。

    未認證的請求可以送任意長的 path，遮罩的成本與長度成正比，所以在任何遮罩工作之前先把原始字串切到
    ``ACCESS_PATH_SCAN_CHARS``（2 × 顯示上限）；切點用 ``_dangling_escape`` 避開不完整的 ``%XX``，丟掉的尾段計入標記。
    丟掉尾段不會洩漏：憑證一定接在標記之後，窗內被切到一半的 token 仍由前綴規則遮住，窗外的字元根本不輸出。
    窗內的結果與 ff3dbc0 相同（不超過窗的 path 逐位元相同）。
    """
    if not isinstance(path, str):
        return _redact_value(path)
    dropped = 0
    if len(path) > ACCESS_PATH_SCAN_CHARS:
        cut = ACCESS_PATH_SCAN_CHARS - _dangling_escape(path, ACCESS_PATH_SCAN_CHARS)
        dropped = len(path) - cut
        path = path[:cut]
    return _truncate_access_path(redact(path), dropped)


def _truncate_access_path(path: Any, dropped: int = 0) -> Any:
    """遮罩後的 path 超過 ACCESS_PATH_MAX_CHARS 就只留前段加標記（``dropped`` 是已先丟掉的尾段字元數）。

    只對「已遮罩」的字串截斷，所以截在哪裡都不會露出憑證（憑證已經是 ``***``）；要避免留下不完整的 ``%x``（否則後面整行
    檢查會把它當成無法判斷的編碼），也不能切開 ``***``（否則第二次遮罩會把 ``*`` 補成 ``***``，結果不同，R3-2）。
    有丟過尾段時一定也切到顯示上限，這樣「結果＋標記」不超過上限＋寬限，重複遮罩不會再截一次。
    """
    if not isinstance(path, str):
        return path
    if not dropped and len(path) <= ACCESS_PATH_MAX_CHARS + ACCESS_PATH_MARKER_SLACK:
        return path
    cut = _safe_cut(path, min(len(path), ACCESS_PATH_MAX_CHARS))
    return _with_marker(path[:cut], dropped + len(path) - cut)


def _scrub_uvicorn_access(record: logging.LogRecord) -> None:
    """uvicorn.access：args 永遠維持 5 元組，只替換含敏感或無法判斷內容的參數。

    AccessFormatter 用 ``client_addr, method, full_path, http_version, status_code = record.args`` 拆參數，
    整句替換（args=()）會讓它拋 ValueError、整行 access log 消失並在 stderr 印 traceback。這裡只動字串參數：
    path 走 redact()（遮成 ``/private_***``，或無法判斷時換成 ``AMBIGUOUS``）；client／method／version 若不是
    預期的短 token 就一併換掉，且連 path 也換掉（避免憑證被拆在相鄰參數之間）。狀態碼等非字串不動。
    """
    client, method, path, version, status = record.args  # type: ignore[misc]
    atoms = [client, method, version]
    clean = all(not isinstance(a, str) or _ACCESS_ATOM_RE.fullmatch(a) for a in atoms)
    if not clean:
        atoms = [a if not isinstance(a, str) or _ACCESS_ATOM_RE.fullmatch(a) else AMBIGUOUS for a in atoms]
        path = AMBIGUOUS
    else:
        path = _scrub_access_path(path)
    record.args = (atoms[0], atoms[1], path, atoms[2], status)
    rendered = _render(record)
    if rendered is not None and redact(rendered) == rendered:
        return
    # 仍有疑慮：path 換成佔位字串；再不行就把所有字串參數換掉（狀態碼維持原樣，形狀不變）。
    record.args = (atoms[0], atoms[1], AMBIGUOUS, atoms[2], status)
    rendered = _render(record)
    if rendered is not None and redact(rendered) == rendered:
        return
    mask = [AMBIGUOUS if isinstance(a, str) else a for a in (client, method, path, version)]
    record.args = (mask[0], mask[1], mask[2], mask[3], status)


def _scrub_message(record: logging.LogRecord) -> None:
    if _is_uvicorn_access(record):
        _scrub_uvicorn_access(record)
        return
    # Format before mutating the template/arguments: a secret can span
    # arguments, and replacing a %s in the template breaks logging itself.
    rendered = _render(record)
    if rendered is None:
        record.msg, record.args = "[redacted: invalid log message]", ()
        return
    safe_rendered = redact(rendered)
    record.msg = _redact_value(record.msg)
    if record.args:
        record.args = _redact_args(record.args)
    if _render(record) != safe_rendered:
        # 憑證跨 args 拆開、或模板被改寫：整句換成遮好的字串。其他 logger 的 Formatter 不依賴 args 形狀。
        record.msg, record.args = safe_rendered, ()


# 每筆 record 在 factory＋logger filter 的整條路徑上只實際遮一次（r2co-1：uvicorn.access 原本遮 4 次；
# r2co-4：重複遮罩讓截斷計數與標記失真）。
#
# 「已遮罩」記在這個 WeakKeyDictionary，不寫在 record 屬性上：呼叫端沒有任何方式可以經由 ``extra=``、
# 自訂屬性或 pickle 讓 record 被當成已遮罩而跳過（也不會讓同名 extra 觸發 makeRecord 的 KeyError，或讓 JSON
# formatter 把標記印出去）。值是遮罩當下的 (msg, args, exc_info, exc_text, stack_info) 參照；之後有人（例如排在前面
# 的第三方 filter、``makeLogRecord`` 之後才指定的 ``exc_info``）把其中任何一個換成別的物件，指紋對不上就會重新遮。
# 不經 factory 建立的 record（第三方直接 ``logging.LogRecord(...)``）不在字典裡，filter 照常遮。
#
# 指紋只比參照，擋不住「同一個物件事後內容變了」，所以以下情況一律**不登記**標記（每一層 handler／filter 都重遮，
# 與 ec82641 相同；R3-1）：
#   - msg 不是純 ``str``（物件的 ``__str__`` 可以事後改變）；
#   - args 是 dict（就地改動身分不變），或 tuple 內有 str／int／float／bool／bytes／None 以外的元素
#     （list、dict、自訂物件、lazy ``__str__``）；
#   - 有 ``exc_info`` 而遮罩後 ``exc_text`` 仍是空的（Formatter 會在 emit 當下才格式化 traceback，例外物件或
#     traceback 事後改變就看不到）。
# 一般的 record（字串 msg＋基本型別 args＋沒有例外）仍然只遮一次。
_scrubbed: weakref.WeakKeyDictionary[logging.LogRecord, tuple[Any, ...]] = weakref.WeakKeyDictionary()
# 精確型別比對（不用 isinstance）：str／int 的子類可以覆寫 ``__str__``／``__format__``。
_PLAIN_TYPES = frozenset({str, int, float, bool, bytes, type(None)})


def _fingerprint(record: logging.LogRecord) -> tuple[Any, ...]:
    return (record.msg, record.args, record.exc_info, record.exc_text, record.stack_info)


def _is_immutable_plain(record: logging.LogRecord) -> bool:
    """這筆 record 的 msg／args／例外在遮罩之後是否「身分不變就代表內容不變」。"""
    if type(record.msg) is not str:
        return False
    args = record.args
    if args is not None and not (type(args) is tuple and all(type(a) in _PLAIN_TYPES for a in args)):
        return False
    return not (record.exc_info and not record.exc_text)


def _already_scrubbed(record: logging.LogRecord) -> bool:
    try:
        mark = _scrubbed.get(record)
    except TypeError:  # 不可 hash／不可弱參照的 record 子類：每次都遮
        return False
    current = _fingerprint(record)
    return mark is not None and len(mark) == len(current) and all(a is b for a, b in zip(mark, current))


def _mark_scrubbed(record: logging.LogRecord) -> None:
    try:
        if _is_immutable_plain(record):
            _scrubbed[record] = _fingerprint(record)
        else:
            _scrubbed.pop(record, None)
    except TypeError:
        pass


def scrub_record(record: logging.LogRecord) -> None:
    """就地遮 record 的 msg／args／例外文字／stack（不可讓 logging 爆掉）。同一筆 record 只實際遮一次。"""
    try:
        if _already_scrubbed(record):
            return
        _scrub_message(record)
        if record.exc_info and not record.exc_text:
            # Formatter 只在 exc_text 為空時才自己格式化 traceback；含憑證就先格式化並遮好，handler 會沿用。
            text = logging.Formatter().formatException(record.exc_info)
            cleaned = redact(text)
            if cleaned != text:
                record.exc_text = cleaned
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        _mark_scrubbed(record)
    except Exception:  # noqa: BLE001
        pass


class PathTokenRedactionFilter(logging.Filter):
    """logger 層的第二道防線（record factory 被換掉時仍遮 uvicorn／httpx 的 log）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        scrub_record(record)
        return True


def _install_record_factory() -> None:
    global _factory_installed
    with _lock:
        if _factory_installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            scrub_record(record)
            return record

        factory._mcp_admin_url_token_scrubber = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)
        _factory_installed = True


def install(extra_loggers: Iterable[str] = ()) -> None:
    """安裝 record factory，並在會帶路徑的 logger 上掛 filter（皆 idempotent）。"""
    _install_record_factory()
    for name in (*PATH_LOGGERS, *extra_loggers):
        target = logging.getLogger(name)
        if not any(isinstance(f, PathTokenRedactionFilter) for f in target.filters):
            target.addFilter(PathTokenRedactionFilter())
