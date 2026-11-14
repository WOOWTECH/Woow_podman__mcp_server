"""負責人 2026-10-25「預設全開」：既有 config.json 在第一次以本映像啟動時，一次性改成全開。

規則（每個 app 共用，值來自 app 的 ``LaunchPolicy.fresh_env``）：

- **標記**：``tools["owner_defaults_20261025"]``。放在 ``tools`` 區段，因為整份還原（``ConfigStore.save``）會保留
  目前的 ``tools``——舊快照不會把標記洗掉、讓遷移重跑。內容 ``{"applied", "source", "at"}``：

  - ``applied``（fc5fa02 起就有，值不變）：``migrated``＝這次有改（原本有限制）；``fresh_install``＝不需要改。
    注意它說的是「需不需要改」，不是安裝歷史：沒有任何限制的舊服務也是 ``fresh_install``。
  - ``source``（2026-10-26 起新寫入的標記才有）：設定從哪裡來，見 :func:`config_source`。
    ``new_config``＝這個 admin 行程自己建立的 config.json（沒有部署 seed 的全新安裝，例如 docker compose）；
    ``deploy_seed``＝第一次以本映像啟動時 config.json 已經存在，但還沒有任何使用痕跡（沒有 MCP token、token 輪替
    紀錄、管理登入紀錄，連線欄位也都是空的）——PaaS 新開的服務就是這樣：chart 的 seed initContainer 剛寫好、admin
    第一次啟動，平台只 seed 管理密碼；從沒有人設定過的舊服務也長這樣，同樣沒有任何要保留的設定；
    ``existing_config``＝第一次以本映像啟動時設定已經有使用痕跡（10-25 之前就在用的服務）。所以「既有設定但不需要
    改」＝``existing_config``＋``fresh_install``，PaaS 新服務＝``deploy_seed``＋``fresh_install``。舊標記沒有這個
    欄位（＝不知道）。
  - ``at``：寫上標記的時間（UTC）。

  讀標記的程式只要看 ``applied``／``at`` 就和以前一樣（例如驗收用的 check_marker.py 照印整個標記）。
- **沒有標記**＝本映像之前建立的設定（舊範本 seed、舊版 admin）：core lifespan 拉起子行程之前，把本產品的全開值
  寫進去——``mcp_server.env`` 以 ``LaunchPolicy.fresh_env`` 覆寫（寫入、破壞性操作、Secret、工具集、多叢集等開關；
  ``LaunchPolicy.fresh_env_union_keys`` 列出的清單鍵例外，取舊值與全開值的聯集、不拿掉管理員加的項目，見
  :func:`union_list_value`）、
  ``tools.disabled = []``、``tools.disabled_operations = {}``；憑證、token、連線網址、port 與其他設定一律不動。標記與
  全開值在同一次原子寫入（同目錄暫存檔＋replace，見 ``ConfigStore.transform``）寫下，記一行 log。
- **已有標記**：什麼都不做——之後管理員在 Settings／Tools 頁設的限制跨重啟、升級都保留。
- **全新安裝**：本行程建立的 config.json 由 ``store.fresh_config()`` 直接帶上標記（``new_config``，不需遷移）；部署
  seed 建立、還沒有標記的新設定（PaaS 新服務），第一次啟動時發現本來就是全開，只寫上標記（``deploy_seed``）。
- **讀不到／壞掉的設定**：``ConfigStore`` 照舊拋 ``ConfigUnavailable``，不遷移、不覆寫（fail safe）。
- **log**：遷移（或只寫標記）的那一行同時寫到容器 stdout（``kubectl logs``／log collector 稽核用）與管理頁 Logs 的
  ring buffer，各一次。admin 沒有設定 root logger，``mcp_admin_core`` 的 logger 掛了 ring buffer 的 handler 之後，
  Python 的 lastResort 也不會再印到 stderr，所以這個 logger 自己掛一個寫 stdout 的 handler（:class:`_StdoutHandler`），
  紀錄照常往上傳到 ring buffer。
"""
from __future__ import annotations

import copy
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)


class _StdoutHandler(logging.StreamHandler):
    """寫到「當下的」``sys.stdout``（容器 stdout＝``kubectl logs``）；測試換掉 ``sys.stdout`` 時也跟著換。"""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(levelname)s:     [%(name)s] %(message)s"))

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        pass


def _install_stdout_audit() -> None:
    """遷移紀錄也寫到 stdout（只掛一次；紀錄仍往上傳給 mcp_admin_core 的 ring buffer handler，不重複）。"""
    if not any(isinstance(handler, _StdoutHandler) for handler in logger.handlers):
        logger.addHandler(_StdoutHandler())
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)  # 「本來就是全開，只寫標記」是 INFO，root 預設 WARNING 也要印


_install_stdout_audit()

MARKER = "owner_defaults_20261025"
# Offline restore records this until a compatible runtime atomically records
# the ordinary marker, without applying the all-enabled migration.
RESTORE_POLICY_HOLD = "restore_guard_preserve_policy_20261112"
DECISION = "負責人 2026-10-25「預設全開」"
APPLIED_FRESH = "fresh_install"
APPLIED_MIGRATED = "migrated"
APPLIED_RESTORE_PRESERVED = "restore_preserved"
#: 標記的 ``source``：這個 admin 行程自己建立的 config.json／已經存在但還沒有使用痕跡的部署 seed／
#: 已經有使用痕跡的既有設定（見 :func:`config_source`）。
SOURCE_NEW_CONFIG = "new_config"
SOURCE_DEPLOY_SEED = "deploy_seed"
SOURCE_EXISTING_CONFIG = "existing_config"
SOURCES = (SOURCE_NEW_CONFIG, SOURCE_DEPLOY_SEED, SOURCE_EXISTING_CONFIG)

#: 只有 admin 行程在使用中才會寫的欄位：設過或換過 MCP token（``mcp_auth_token``；10-23 起另有 generation，
#: 第二次之後的輪替有 ``token_history``）、登入過或改過管理密碼（``admin_session_generation``，10-11 起）。
#: 部署 seed 不會帶這些（PaaS 平台的 ``config.sensitive`` 只有 ``admin_password``，n8n 另有 chart 自產的
#: ``proxy.bearer_token``／``mcp_server.env.AUTH_TOKEN``，都不在這裡）。平台之後若改成在 seed 帶 MCP token
#: 或連線設定，新服務會被判成 ``existing_config``，要一起改這裡（例如讓 seed 帶明確的來源）。
_USE_MARK_KEYS = ("mcp_auth_token", "mcp_auth_generation", "token_history", "admin_session_generation")


def marker_value(applied: str, source: str) -> dict[str, str]:
    return {"applied": applied, "source": source, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _has_value(value: Any) -> bool:
    """有實際內容：``None``、``False``、空白字串、空的 list／dict 都不算。"""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def config_source(cfg: Mapping[str, Any]) -> str:
    """第一次以本映像啟動、還沒有標記的設定是 ``deploy_seed`` 還是 ``existing_config``（不看工具與 env 開關）。

    任何使用痕跡（:data:`_USE_MARK_KEYS` 有值、MCP token 被撤銷過、``connection`` 有任何非空的值）＝
    ``existing_config``；都沒有＝``deploy_seed``。chart 預設的 ``connection.gateway_url``／``dashboard_url``
    是空字串，不算。只用於標記與 Settings 頁的說明，不影響要不要改成全開（那是 ``applied``）。
    """
    if cfg.get("mcp_auth_revoked") is True:
        return SOURCE_EXISTING_CONFIG
    if any(_has_value(cfg.get(key)) for key in _USE_MARK_KEYS):
        return SOURCE_EXISTING_CONFIG
    connection = cfg.get("connection")
    if isinstance(connection, Mapping) and any(_has_value(value) for value in connection.values()):
        return SOURCE_EXISTING_CONFIG
    return SOURCE_DEPLOY_SEED


def _list_items(value: Any) -> list[str]:
    items: list[str] = []
    for part in value.split(",") if isinstance(value, str) else []:
        item = part.strip()
        if item and item not in items:
            items.append(item)
    return items


def union_list_value(current: Any, fresh: str) -> str:
    """逗號分隔清單的聯集（去空白、去重，結果穩定、重複套用不變）。

    舊值已涵蓋全部全開項目就照舊值（不動管理員的順序）；否則以全開值的順序為準，再照原順序補上舊值多出來的項目
    （例如 kiali、netobserv）。改版前的出廠值（k8s 的 ``core,config,helm``）因此正好變成新安裝的全開值。
    """
    fresh_items, current_items = _list_items(fresh), _list_items(current)
    if current_items and set(fresh_items) <= set(current_items):
        return ",".join(current_items)
    return ",".join(fresh_items + [item for item in current_items if item not in fresh_items])


def all_on_env(env: Mapping[str, Any], fresh_env: Mapping[str, str],
               union_keys: frozenset[str] | set[str] = frozenset()) -> dict[str, str]:
    """全開之後 ``fresh_env`` 各鍵的值：一般鍵＝全開值；清單鍵（*union_keys*）＝舊值與全開值的聯集。"""
    return {key: union_list_value(env.get(key), value) if key in union_keys else value
            for key, value in fresh_env.items()}


def all_on_changes(cfg: Mapping[str, Any], fresh_env: Mapping[str, str],
                   union_keys: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """``cfg`` 要改成全開時會動到的項目（只列名稱，不列值）。"""
    changes: list[str] = []
    env = ((cfg.get("mcp_server") or {}).get("env") or {})
    target = all_on_env(env, fresh_env, union_keys)
    changed_env = sorted(key for key, value in target.items() if env.get(key) != value)
    if changed_env:
        changes.append("mcp_server.env[" + ", ".join(changed_env) + "]")
    tools = cfg.get("tools") or {}
    disabled = tools.get("disabled") or []
    if disabled:
        count = len(disabled) if isinstance(disabled, list) else len([t for t in str(disabled).split(",") if t.strip()])
        changes.append(f"tools.disabled ({count} → 0)")
    operations = tools.get("disabled_operations") or {}
    if operations:
        changes.append(f"tools.disabled_operations ({len(operations)} → 0)")
    return changes


def all_on_config(cfg: Mapping[str, Any], fresh_env: Mapping[str, str], applied: str,
                  union_keys: frozenset[str] | set[str] = frozenset(), source: str | None = None) -> dict[str, Any]:
    """回傳全開後的新設定（不改傳入的 ``cfg``）：只動 env 開關、兩個停用清單與標記。

    *source* 不給就依 ``cfg`` 判斷（:func:`config_source`）；遷移只會碰到已經存在的設定，所以不會是 ``new_config``。
    """
    updated = copy.deepcopy(dict(cfg))
    mcp_server = dict(updated.get("mcp_server") or {})
    env = dict(mcp_server.get("env") or {})
    env.update(all_on_env(env, fresh_env, union_keys))
    mcp_server["env"] = env
    tools = dict(updated.get("tools") or {})
    tools["disabled"] = []
    tools["disabled_operations"] = {}
    tools[MARKER] = marker_value(applied, source or config_source(cfg))
    updated["mcp_server"] = mcp_server
    updated["tools"] = tools
    return updated


async def apply_owner_defaults(store=None) -> list[str] | None:
    """沒有標記時套用全開並寫上標記；還原保留標記則保留原政策。

    回傳改動清單（已有標記或保留還原政策回 None）。

    讀取失敗（壞檔、懸空 symlink、型別錯）時 ``ConfigUnavailable`` 往上拋，呼叫端（lifespan）因此不會啟動子行程。
    """
    from .config import get_config_store
    from .launch_policy import get_launch_policy

    store = store or get_config_store()
    policy = get_launch_policy()
    fresh_env = dict(policy.fresh_env) if policy is not None else {}
    union_keys = policy.fresh_env_union_keys if policy is not None else frozenset()
    result: dict[str, list[str]] = {}

    def migrate(cfg: dict[str, Any]) -> dict[str, Any] | None:
        tools = cfg.get("tools") or {}
        if RESTORE_POLICY_HOLD in tools and MARKER not in tools:
            # Only the restore workflow sets this after validating both policy
            # sections. Preserve the old policy and product env byte-for-byte.
            updated = copy.deepcopy(cfg)
            updated["tools"].pop(RESTORE_POLICY_HOLD)
            updated["tools"][MARKER] = marker_value(APPLIED_RESTORE_PRESERVED, SOURCE_EXISTING_CONFIG)
            return updated
        if MARKER in tools:
            return None
        changes = all_on_changes(cfg, fresh_env, union_keys)
        result["changes"] = changes
        result["source"] = source = config_source(cfg)
        return all_on_config(cfg, fresh_env, APPLIED_MIGRATED if changes else APPLIED_FRESH, union_keys, source)

    await store.transform(migrate)
    if "changes" not in result:
        return None
    changes, source = result["changes"], result["source"]
    if changes:
        logger.warning("%s：既有設定已一次性改成全開（%s）；之後管理員的限制會保留（source=%s）",
                       DECISION, "；".join(changes), source)
    else:
        logger.info("%s：設定本來就是全開，只寫上標記 %s（source=%s）", DECISION, MARKER, source)
    return changes


def token_facts(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Settings 頁那一行要的 MCP token 事實（不含 token 本身）：``token_state`` 與 ``token_auto_issued``。

    ``token_state`` 與 Tokens 頁（``routers.tokens``）同一個判斷：撤銷過＝``revoked``、有 token＝``active``、否則
    ``unconfigured``。``token_auto_issued``＝現在這個 token 是第一次啟動時自動發的（``initial_token``：最新一筆
    ``token_history`` 是 ``auto_issue``、而且還在用）——管理員自己輪替過、撤銷過，或第二輪映像開出後自己 Rotate 的服務都不是
    （審查第三輪 G4：Settings 頁不能只看標記的 ``source`` 就說「已自動產生」）。
    """
    from .initial_token import AUTO_ISSUE_ACTION  # 延後 import：initial_token import 本模組

    revoked = bool(cfg.get("mcp_auth_revoked"))
    active = not revoked and bool(str(cfg.get("mcp_auth_token") or "").strip())
    history = cfg.get("token_history") or []
    latest = history[0] if isinstance(history, list) and history and isinstance(history[0], Mapping) else {}
    return {
        "token_state": "revoked" if revoked else "active" if active else "unconfigured",
        "token_auto_issued": active and latest.get("action") == AUTO_ISSUE_ACTION,
    }


def owner_defaults_view(cfg: Mapping[str, Any]) -> dict[str, Any] | None:
    """給管理頁顯示預設決定或還原時保留政策的狀態，加上 MCP token 的事實（:func:`token_facts`）。

    ``source`` 舊標記沒有，回 None。
    """
    tools = cfg.get("tools") or {}
    value = tools.get(MARKER)
    if value is None:
        if RESTORE_POLICY_HOLD not in tools:
            return None
        value = {"applied": APPLIED_RESTORE_PRESERVED, "source": SOURCE_EXISTING_CONFIG}
    view = {"decision": DECISION, "applied": APPLIED_FRESH, "source": None, "at": None}
    if isinstance(value, dict):
        view["applied"] = str(value.get("applied") or APPLIED_FRESH)
        source = value.get("source")
        view["source"] = source if source in SOURCES else None
        view["at"] = value.get("at")
    view.update(token_facts(cfg))
    return view
