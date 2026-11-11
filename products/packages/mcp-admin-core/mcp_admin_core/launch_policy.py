"""MCP 子行程啟動政策（woow_paas_platform#1530）。

過去 ``config.json`` 的 ``mcp_server.command``／``args``／``env`` 與 ``connection`` 的任意鍵都可以從
管理頁（``PUT /api/settings/mcp_server``、``PUT /api/settings/connection``）改掉，存檔後直接
``create_subprocess_exec``：持有管理密碼的人可以在容器裡執行任意程式，或用 ``NODE_OPTIONS``／
``LD_PRELOAD``／``WEBHOOK_SECURITY_MODE`` 這類環境變數改變子行程行為。

這裡讓每個 app 在程式碼裡宣告自己的子行程：

- ``command``／``args``：固定由 app 決定（``args`` 可含 ``{port}``，啟動時代入 ``mcp_server.port``）。
  ``config.json`` 裡的 command／args 只剩顯示用途，不再被執行；舊 seed（例如 opendesign 的
  supergateway、n8n 的舊指令）也一律以 app 預設啟動。
- ``connection_keys``：``connection`` 允許存的鍵；只有這些鍵會以 ``{env_prefix}{KEY}`` 注入子行程
  （``non_env_connection_keys`` 例外：只存不注入，由 ``derived_env`` 換算）。
- ``env_keys``：``mcp_server.env`` 會注入子行程的鍵（部署 seed 的值，如 n8n 的 ``AUTH_TOKEN``）。
- ``editable_env_keys``：管理頁可以修改的 ``mcp_server.env`` 鍵（Settings 頁的開關）。
- ``derived_env``：依 ``config.json`` 推導出的環境變數（例如 n8n 把 Permissions 轉成上游的
  ``DISABLED_TOOL_OPERATIONS``）。
- ``url_connection_keys``：``connection`` 裡的後端網址欄位。存檔時（產品的 ``PUT /api/config/connection`` 與共用的
  ``PUT /api/settings/connection``）用對齊子行程（上游 n8n-mcp）規則的 SSRF 檢查（``util/ssrf.check_url``，模式讀部署端的
  ``WEBHOOK_SECURITY_MODE``）檢查，被擋就回 400、不存檔（``connection_apply.refuse_blocked_urls``）。
- ``fresh_env``：本行程自己建立 ``config.json``（全新安裝、沒有部署 seed，例如 docker compose）時
  ``mcp_server.env`` 的起始值。負責人 2026-10-25 決定 MCP 新安裝一律「預設全開」：寫入、破壞性操作
  與 Secret 存取的開關在這裡給開啟值。只作用於新建的檔——既有的 ``config.json``（包括部署 seed 建立的）
  一律不動，缺鍵時仍由各 app 的 ``derived_env``／``seed_defaults`` 補上既有服務原本的值。
- ``fresh_env_union_keys``：``fresh_env`` 裡以逗號分隔的清單（例如 k8s 的 ``K8S_MCP_TOOLSETS``）。既有設定一次性
  改成全開時（``owner_defaults.union_list_value``）取「舊值與全開值的聯集」——舊值已涵蓋全開項目就不動；否則以
  全開值的順序為準、再照原順序補上舊值多出來的項目、去重——不整個覆寫，管理員自己加的項目（kiali、netobserv…）
  不會被拿掉。
- ``required_connection``：子行程缺了就起不來的連線欄位，``(鍵, Connection 頁上的標籤)``，依 Connection 頁的順序
  （例如 Nextcloud 的網址／帳號／app password：缺任何一個子行程就 exit 2）。有欄位是空的時，閘道對 MCP 用戶端回的
  佔位 MCP 伺服器與 ``/healthz`` 的 ``mcp`` 欄位說「尚未設定」並列出缺的欄位（``mcp_admin_core/setup_status.py``）。
  **只列缺了真的起不來的欄位**：沒有連線也能啟動的產品（n8n 文件模式、odoo、litellm、hermes…）不要列，否則子行程
  在跑時 ``/healthz`` 也會報「尚未設定」、子行程因別的原因結束時會被誤報成缺設定。**只認 config.json 的
  ``connection``**：子行程另外繼承容器的 env（``process._build_subprocess_env`` 以 ``os.environ`` 為底），若有部署改用
  pod env 提供這些連線（例如 ``NEXTCLOUD_MCP_BASE_URL``），子行程會正常在跑，但 ``/healthz`` 會一直報
  ``not_configured``、佔位 session 也不會轉成 404。目前 chart、docker compose 與 README 都只經 config.json 注入；要支援
  env 提供連線時，把繼承的 env 一起算進 :meth:`LaunchPolicy.missing_connection`。
- ``backend_connection``：後端連線欄位，形狀與驗證同 ``required_connection``（``(鍵, Connection 頁上的標籤)``，依
  Connection 頁的順序），但意思不同：子行程**沒有它也起得來**，只是沒接上後端——工具呼叫會失敗或只剩部分功能（例如
  odoo 的網址、資料庫、帳號、API Key：缺任何一個每支 Odoo 工具都失敗；n8n 的 API 網址與 API key：缺一個上游就只開 7 支
  文件工具）。有欄位是空的時 ``/healthz`` 多回 ``"backend": "missing"``（都填了是 ``"configured"``），給平台顯示較輕的
  「尚未設定後端連線」提示；``mcp`` 欄位、閘道轉發與佔位 MCP 都不受影響（子行程照常在跑、照常轉發）。``configured``
  只代表欄位都填了，不代表連得上或帳密正確（那是 Connection 頁 Test 的事）。一個鍵只能是其中一種（缺了起不來就放
  ``required_connection``），也一樣只認 config.json 的 ``connection``。沒宣告的產品 ``/healthz`` 沒有 ``backend``。

沒有宣告政策的情況（core 單元測試、自行組裝的 app）維持舊行為，但仍套 ``BLOCKED_ENV_NAMES``
黑名單，且管理 API 一樣不接受修改 command／args。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# 不論有沒有政策都不允許由設定注入的環境變數：會改變直譯器／動態連結器行為、或可關掉上游防護。
BLOCKED_ENV_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "ENV",
        "BASH_ENV",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
        "PERL5OPT",
        "PERL5LIB",
        "RUBYOPT",
        "RUBYLIB",
        "GCONV_PATH",
        "JWT_SECRET",
        "MCP_ADMIN_CONFIG",
        "WEBHOOK_SECURITY_MODE",
        "USE_FIXED_HTTP",
        "MCP_MODE",
        "HOST",
        "PORT",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
    }
)
_BLOCKED_ENV_PREFIXES: tuple[str, ...] = ("LD_", "DYLD_", "PYTHON", "NPM_CONFIG_", "UV_")

# 子行程不需要、也不該看到的 admin 程序環境變數（JWT 簽章金鑰）。
STRIPPED_PARENT_ENV: frozenset[str] = frozenset({"JWT_SECRET"})

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_blocked_env_name(name: str) -> bool:
    """黑名單判定（大小寫不敏感）；名稱不合法（含 ``=``、空白等）也視為封鎖。"""
    if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
        return True
    upper = name.upper()
    if upper in BLOCKED_ENV_NAMES:
        return True
    if upper.startswith("LC_") or upper == "LANG":
        return False
    return upper.startswith(_BLOCKED_ENV_PREFIXES) or (
        upper.startswith("HTTP") and upper.endswith("_PROXY")
    )


@dataclass(frozen=True)
class LaunchPolicy:
    """單一 app 的子行程政策（見模組說明）。"""

    command: str
    args: tuple[str, ...] = ()
    env_prefix: str = ""
    connection_keys: frozenset[str] = field(default_factory=frozenset)
    # connection 允許存、但不直接注入子行程的鍵（由 derived_env 換算，例如 n8n 的 session 設定）
    non_env_connection_keys: frozenset[str] = field(default_factory=frozenset)
    env_keys: frozenset[str] = field(default_factory=frozenset)
    editable_env_keys: frozenset[str] = field(default_factory=frozenset)
    derived_env: Callable[[Mapping[str, Any]], Mapping[str, str]] | None = None
    # 存檔時要過 SSRF 檢查的後端網址欄位（見模組說明）；必須是 connection_keys 的子集合。
    url_connection_keys: frozenset[str] = field(default_factory=frozenset)
    # 全新安裝（本行程建立 config.json）時 mcp_server.env 的起始值，見模組說明；不參與 hash。
    fresh_env: Mapping[str, str] = field(default_factory=dict, hash=False)
    # fresh_env 中以逗號分隔的清單鍵：一次性遷移時取舊值與全開值的聯集（見模組說明）。必須是 fresh_env 的鍵。
    fresh_env_union_keys: frozenset[str] = field(default_factory=frozenset)
    # 子行程缺了就起不來的連線欄位：((鍵, Connection 頁上的標籤), ...)，見模組說明。鍵必須是會注入子行程的 connection_keys。
    # 只看 config.json 的 connection（不看子行程繼承的容器 env），見模組說明。
    required_connection: tuple[tuple[str, str], ...] = ()
    # 後端連線欄位：子行程起得來、但沒填就等於沒接後端（工具呼叫失敗或只剩部分功能），形狀與驗證同 required_connection，
    # 鍵不可與 required_connection 重複。只給 /healthz 的 backend 欄位用，見模組說明。
    backend_connection: tuple[tuple[str, str], ...] = ()
    # Optional product validator shared with its dedicated connection route.
    # It receives only supplied, allowlisted keys: omitted fields stay omitted.
    normalize_connection: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("LaunchPolicy.command is required")
        if not self.non_env_connection_keys <= self.connection_keys:
            raise ValueError("non_env_connection_keys must be a subset of connection_keys")
        if not self.url_connection_keys <= self.connection_keys:
            raise ValueError("url_connection_keys must be a subset of connection_keys")
        stray = self.editable_env_keys - self.env_keys
        if stray:
            raise ValueError(f"editable_env_keys must be a subset of env_keys: {sorted(stray)}")
        stray_fresh = set(self.fresh_env) - self.env_keys
        if stray_fresh:
            raise ValueError(f"fresh_env keys must be a subset of env_keys: {sorted(stray_fresh)}")
        if not all(isinstance(v, str) for v in self.fresh_env.values()):
            raise ValueError("fresh_env values must be strings (child environment values)")
        stray_union = set(self.fresh_env_union_keys) - set(self.fresh_env)
        if stray_union:
            raise ValueError(f"fresh_env_union_keys must be keys of fresh_env: {sorted(stray_union)}")
        required_keys = self._checked_fields("required_connection", self.required_connection)
        backend_keys = self._checked_fields("backend_connection", self.backend_connection)
        overlap = set(required_keys) & set(backend_keys)
        if overlap:
            raise ValueError(
                "a connection key is either required_connection (the child cannot start without it) or "
                f"backend_connection (the child starts but has no backend), not both: {sorted(overlap)}")
        bad = [k for k in self.env_keys if is_blocked_env_name(k)]
        bad += [
            k
            for k in self.connection_keys - self.non_env_connection_keys
            if is_blocked_env_name(f"{self.env_prefix}{k.upper()}")
        ]
        if bad:
            raise ValueError(f"LaunchPolicy declares blocked env names: {sorted(bad)}")

    def build_args(self, port: int) -> list[str]:
        return [a.replace("{port}", str(port)) for a in self.args]

    def build_command(self, port: int) -> list[str]:
        return [self.command, *self.build_args(port)]

    def env_name_for_connection_key(self, key: str) -> str:
        return f"{self.env_prefix}{key.upper()}"

    def missing_connection(self, connection: Any) -> list[tuple[str, str]]:
        """``required_connection`` 裡目前沒填的欄位（沒有這個鍵、None、空字串或只有空白），依宣告順序。"""
        return _blank_fields(self.required_connection, connection)

    def missing_backend_connection(self, connection: Any) -> list[tuple[str, str]]:
        """``backend_connection`` 裡目前沒填的欄位，判斷同 :meth:`missing_connection`（不是 Mapping 就當作全都沒填）。"""
        return _blank_fields(self.backend_connection, connection)

    def _checked_fields(self, name: str, fields: Any) -> list[str]:
        """驗證 ``required_connection``／``backend_connection`` 的宣告，回傳鍵（依宣告順序）。

        要 tuple（政策是 frozen dataclass，要能 hash）、每項是兩個非空字串的 ``(鍵, 標籤)``、鍵不重複、鍵是會注入子行程的
        connection_keys（``non_env_connection_keys`` 只存不注入，子行程看不到，宣告了也沒有意義）。
        """
        if not isinstance(fields, tuple):
            raise ValueError(f"{name} must be a tuple of (key, label) pairs")
        for entry in fields:
            if not (isinstance(entry, tuple) and len(entry) == 2
                    and all(isinstance(value, str) and value.strip() for value in entry)):
                raise ValueError(f"{name} entries must be (key, label) pairs of non-empty strings")
        keys = [key for key, _label in fields]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{name} keys must be unique")
        stray = set(keys) - (self.connection_keys - self.non_env_connection_keys)
        if stray:
            raise ValueError(f"{name} keys must be connection_keys injected into the child: {sorted(stray)}")
        return keys


def _blank_fields(fields: tuple[tuple[str, str], ...], connection: Any) -> list[tuple[str, str]]:
    stored = connection if isinstance(connection, Mapping) else {}
    return [(key, label) for key, label in fields if _is_blank(stored.get(key))]


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


_policy: LaunchPolicy | None = None


def set_launch_policy(policy: LaunchPolicy | None) -> None:
    """由 ``create_app(launch_policy=...)`` 設定；測試可傳 None 還原。"""
    global _policy  # noqa: PLW0603
    _policy = policy


def get_launch_policy() -> LaunchPolicy | None:
    return _policy


def coerce_port(value: Any, default: int = 8000) -> int:
    """``mcp_server.port`` 轉成合法 port；不合法就退回 default。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default
