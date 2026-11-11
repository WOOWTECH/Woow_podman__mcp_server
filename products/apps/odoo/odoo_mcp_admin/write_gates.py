"""Odoo MCP 四個寫入開關的語意：``execute_method``／``chatter_post`` 的決定，以及五支寫入相關工具與 health_check 的說明。

建置期修補 ``apps/odoo/patches/fix_direct_writes.py`` 把上游 odoo-mcp 1.3.0 的 ``execute_method``、``chatter_post`` 與
五支工具（``execute_method`` 的 ``method`` 參數、``chatter_post``、``preview_write``、``validate_write``、
``execute_approved_write``）的說明、``health_check`` 的 ``runtime.notes`` 都接到這裡。這裡只有純函式：不讀環境變數、
不 import ``odoo_mcp``——開關值由修補後的 ``odoo_mcp.write_policy.write_switches()`` 以「強制那個開關的判斷式」讀出
（審核寫入＝上游 ``writes_enabled``、直接寫入＝修補加的 ``direct_writes_enabled``、未審查方法＝上游
``truthy_env("ODOO_MCP_ALLOW_UNKNOWN_METHODS")``、chatter＝上游 ``chatter_direct_enabled``）再傳進來，所以決定、說明與
health_check 永遠是同一組值。說明在子行程註冊工具時算一次（改設定會重啟子行程）。

語意（負責人 2026-10-25「預設全開」之後，管理員自己收緊的設定要真的有效；第三輪 G1）：

0. **唯讀方法＝審過的讀取清單** :data:`REVIEWED_READ_METHODS`：上游 odoo-mcp 1.3.0 ``READ_ONLY_METHODS`` 的精確名單，
   加上 Odoo 18／19 自己標 ``@api.readonly`` 的讀取 API（read_group、search_fetch、get_views、web client 的 web_read
   等）。上游 ``classify_method_safety`` 另外依名字把所有 ``get_*``／``_get_*`` 判成唯讀，但名字不保證不寫：Odoo 18
   標準的 ``portal.mixin.get_portal_url``（sale.order、account.move、purchase.order、project.task…都有）在 access_token
   空的時候先 ``sudo().write`` 一個再回公開分享連結；WoowTech 自己的 addon 也有找不到就建立紀錄、或打外部 API 後寫回的
   ``get_*``。所以不在清單上的方法——包括這些 ``get_*``——一律當成未審查方法（審查第三輪 G1）。只有直接寫入與未審查
   方法都開（任何方法都直接跑）時，它們照上游當讀取直接跑、也不讀白名單。
1. ``ODOO_MCP_ENABLE_WRITES``（Enable Approved Writes）是**總閘門**：關掉時任何工具都不寫——``execute_method`` 只跑
   審過的讀取方法，其他一律拒絕（含 create／write／unlink、未審查方法、不在清單上的 get_*、已列入白名單的方法、chatter
   訊息方法）；``chatter_post`` 第一次呼叫就拒絕（不回預覽）；``execute_approved_write`` 照上游拒絕。
   ``preview_write``／``validate_write`` 不寫入，照常回結果，但說明寫明拿到的核准無法執行。
2. ``ODOO_MCP_ALLOW_DIRECT_WRITES``（Allow Direct Writes）關＝``execute_method`` 只直接執行審過的讀取方法與白名單：
   create／write／unlink 照舊要走 preview_write → validate_write → execute_approved_write；未審查的副作用／未知方法
   （action_*、button_*、copy、toggle_active、不在清單上的 get_*…）也拒絕並說明（此時
   ``ODOO_MCP_ALLOW_UNKNOWN_METHODS`` 沒有作用）；只有審查過、列入白名單（``ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS`` 或
   政策檔）的 ``model.method`` 照跑。
3. ``ODOO_MCP_ALLOW_UNKNOWN_METHODS``（Allow Unreviewed Methods）關＝照上游擋未審查的副作用／未知方法（白名單照跑），
   錯誤訊息與上游逐字相同；不在讀取清單上的 get_* 也算未審查方法（上游會依名字放行）。
4. ``MCP_CHATTER_DIRECT``（Direct Chatter Post）關＝``chatter_post`` 先回預覽與核准、帶 approval 與 ``confirm=true``
   才張貼；``execute_method`` 也不跑**在 chatter 張貼訊息的呼叫**（:func:`is_chatter_call`，白名單也不例外），改指向
   ``chatter_post``：``message_`` 開頭的 mail.thread 方法（message_post、message_post_with_source、message_notify、
   message_subscribe…）、建立或修改訊息本身（mail.message、chatter 撰寫器 mail.compose.message、排程訊息
   mail.scheduled.message 上任何讀取以外的呼叫——create／write 也算）、完成活動並把結果貼上 chatter 的方法（mail.activity 的
   action_done／action_feedback 與 *_schedule_next、action_done_redirect_to_other、指派通知 action_notify，任何 model 的
   activity_feedback、activity_send_mail），以及評分的 rating_apply、rating_send_request（審查第三輪 G1 的 chatter 部分；
   對照 odoo/odoo 18.0 的 mail、rating 原始碼）。**不在這個開關範圍**的：其他呼叫改資料時 Odoo 自己在 chatter 留的紀錄
   （欄位追蹤、action_confirm 這類業務方法的訊息、建立活動時的指派通知）——那些由直接寫入與未審查方法兩個開關管。

優先順序：審過的讀取方法永遠可跑 → 總閘門 → chatter 張貼呼叫看 chatter 開關 → create／write／unlink 看直接寫入 →
（任何方法都直接跑時）上游依名字判成唯讀的方法 → 白名單 → 直接寫入 → 未審查方法。四個都開（全開）時每一步都放行，
行為與第二輪（8c825a5）逐位元相同；全開時五支工具的說明也與第二輪相同（``packages/mcp-admin-core/tests/
test_odoo_write_gates.py`` 以 tools/list 的 digest 鎖住）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

#: 上游 ``odoo_mcp.diagnostics.DESTRUCTIVE_METHODS``（1.3.0）：execute_method 的「標準寫入」。
STANDARD_WRITES = frozenset({"create", "write", "unlink"})
#: chatter 訊息方法：mail.thread 的 ``message_*`` API（貼文、通知、追蹤者、郵件閘道建立／更新紀錄）。
CHATTER_METHOD_PREFIX = "message_"
#: 本身就是 chatter 訊息的 model：上面任何讀取以外的呼叫（create、write、copy、action_send_mail、post_message…）都等於
#: 張貼或改寫訊息（odoo/odoo 18.0：mail/models/mail_message.py、mail/wizard/mail_compose_message.py、
#: mail/models/mail_scheduled_message.py）。
CHATTER_MESSAGE_MODELS = frozenset({"mail.message", "mail.compose.message", "mail.scheduled.message"})
#: 任何 model 都可能有、會在紀錄的 chatter 張貼的方法：mail.activity.mixin 的 activity_feedback（完成活動、貼上回饋）與
#: activity_send_mail（以範本 message_post_with_source），rating 的 rating_apply（貼評分與回饋）與 rating_send_request
#: （以範本張貼評分邀請）。
CHATTER_RECORD_METHODS = frozenset({"activity_feedback", "activity_send_mail", "rating_apply", "rating_send_request"})
#: 特定 model 上會在 chatter 張貼的方法：mail.activity 完成活動（都經 ``_action_done`` 貼上「已完成」訊息）與指派通知
#: （``action_notify`` 經 message_notify）。
CHATTER_MODEL_METHODS = {
    "mail.activity": frozenset({
        "action_done", "action_done_redirect_to_other", "action_feedback", "action_done_schedule_next",
        "action_feedback_schedule_next", "action_notify",
    }),
}

#: 上游 ``odoo_mcp.diagnostics.READ_ONLY_METHODS``（1.3.0）的精確名單（不含上游另外依名字放行的 ``get_*``／``_get_*``）。
UPSTREAM_READ_ONLY_METHODS = (
    "search", "search_count", "search_read", "read", "fields_get", "name_get", "name_search", "context_get",
)
#: 對照 Odoo 原始碼審過、Odoo 自己標 ``@api.readonly`` 的讀取 API（odoo/odoo 18.0：``odoo/models.py`` 的 search_fetch、
#: read_group，``odoo/addons/base/models/ir_ui_view.py`` 的 get_views，``addons/web/models/models.py`` 的 web_read、
#: web_search_read、web_read_group、read_progress_bar；19.0 的 web 另有 web_name_search、formatted_read_group、
#: formatted_read_grouping_sets）。``@api.readonly`` 的方法可以在唯讀 cursor（資料庫唯讀副本）上跑，本來就不能寫入。
#: default_get 不在這裡：欄位預設值可以是任何程式（例如取 ir.sequence 的下一號會推進序號）。
ODOO_READONLY_API_METHODS = (
    "search_fetch", "read_group", "get_views", "web_read", "web_search_read", "web_read_group", "web_name_search",
    "read_progress_bar", "formatted_read_group", "formatted_read_grouping_sets",
)
#: 審過的讀取方法：收緊之後 ``execute_method`` 唯一當成「唯讀」直接跑的方法（第三輪 G1）。
REVIEWED_READ_METHODS = frozenset(UPSTREAM_READ_ONLY_METHODS + ODOO_READONLY_API_METHODS)
#: 說明與拒絕訊息裡列出的讀取方法（固定順序）。
_READ_LIST = (
    "search, search_read, search_count, search_fetch, read, read_group, fields_get, name_search, name_get, "
    "context_get, get_views and the web client reads web_read, web_search_read, web_read_group, web_name_search, "
    "read_progress_bar, formatted_read_group and formatted_read_grouping_sets"
)
#: 依名字看起來是讀取、但不在清單上的 get_*（上游判成 read_only）為什麼不算讀取。
_GET_NOT_TRUSTED = (
    "get_* methods that are not on this list are not treated as reads, because some of them write (for "
    "example get_portal_url creates a share token)"
)
#: 清單以外的讀取改用哪些專用的唯讀工具（審查第三輪 G2：說明與拒絕訊息都要指出替代做法）。
_OTHER_READS = "for other reads use search_records, read_record or aggregate_records"


@dataclass(frozen=True)
class Switches:
    """四個寫入開關的值（由 ``odoo_mcp.write_policy.write_switches()`` 讀出）。"""

    enable: bool  # ODOO_MCP_ENABLE_WRITES：總閘門
    direct: bool  # ODOO_MCP_ALLOW_DIRECT_WRITES：execute_method 直接寫入
    unknown: bool  # ODOO_MCP_ALLOW_UNKNOWN_METHODS：execute_method 跑未審查方法
    chatter: bool  # MCP_CHATTER_DIRECT：chatter 直接張貼


def is_chatter_method(method: str) -> bool:
    """``execute_method`` 的這個方法是不是 chatter 訊息方法（mail.thread 的 ``message_*``）。"""
    return method.startswith(CHATTER_METHOD_PREFIX)


def is_chatter_call(model: str, method: str) -> bool:
    """``execute_method`` 的這個呼叫會不會在 chatter 張貼（或改寫）訊息：``MCP_CHATTER_DIRECT`` 關時要改走 ``chatter_post``。

    範圍見模組說明第 4 條：message_* 方法、訊息 model 上讀取以外的呼叫、完成活動與評分的張貼方法。其他呼叫改資料時 Odoo
    順帶留下的 chatter 紀錄（欄位追蹤、業務方法）不算。
    """
    if is_chatter_method(method) or method in CHATTER_RECORD_METHODS:
        return True
    if model in CHATTER_MESSAGE_MODELS:
        return not is_reviewed_read(method)
    return method in CHATTER_MODEL_METHODS.get(model, ())


def is_reviewed_read(method: str) -> bool:
    """這個方法在審過的讀取清單上（:data:`REVIEWED_READ_METHODS`）：收緊之後也照常直接跑。"""
    return method in REVIEWED_READ_METHODS


def _every_method_runs(switches: Switches) -> bool:
    """總閘門、直接寫入、未審查方法都開：任何方法都直接跑（chatter 方法另看 chatter 開關）。"""
    return switches.enable and switches.direct and switches.unknown


def unknown_methods_run(switches: Switches) -> bool:
    """未審查方法實際上會不會直接跑：總閘門、直接寫入、未審查方法三個都開才會（只開未審查方法沒有作用）。

    ``health_check`` 的 ``runtime.broad_unknown_method_mode`` 的 ``enabled``／``risk`` 用這個值（審查第三輪 G3：上游只看
    ``ODOO_MCP_ALLOW_UNKNOWN_METHODS``，總閘門或直接寫入關時仍回 ``enabled: true``、``risk: "broad"``）；
    ``runtime.unknown_execute_method_enabled`` 仍是開關本身的值。
    """
    return _every_method_runs(switches)


def _classification(method: str, safety: Dict[str, Any]) -> Dict[str, Any]:
    """拒絕時附上的分類：上游的結果；上游依名字判成唯讀、但不在審過清單上的，另外標 ``reviewed_read_only: false``。"""
    if safety.get("safety") == "read_only" and not is_reviewed_read(method):
        return {**safety, "reviewed_read_only": False}
    return safety


# ---- execute_method 的拒絕訊息 ------------------------------------------------------------------

#: 直接寫入關時的 create／write／unlink（第二輪起的文字，不變）。
STANDARD_WRITE_REFUSAL = (
    "Direct execute_method blocks create/write/unlink. Use "
    "preview_write -> validate_write -> execute_approved_write, "
    "or set ODOO_MCP_ALLOW_DIRECT_WRITES=1."
)
#: 未審查方法關時（上游 odoo-mcp 1.3.0 逐字）。
UNREVIEWED_REFUSAL = (
    "Unreviewed side-effect methods are blocked by default. Review "
    "custom source, then add the exact 'model.method' to the policy "
    "file (ODOO_MCP_POLICY_FILE, default ./odoo_mcp_policy.json, "
    "re-read on every request — see odoo_mcp_policy.json.example) "
    "or to ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS=model.method, or "
    "set ODOO_MCP_ALLOW_UNKNOWN_METHODS=1 only for trusted "
    "deployments."
)
#: 總閘門關（措辭比照上游 execute_approved_write 的 "write execution disabled; set ODOO_MCP_ENABLE_WRITES=1 to enable"）。
WRITES_DISABLED_REFUSAL = (
    "write execution disabled; set ODOO_MCP_ENABLE_WRITES=1 to enable. While writes are off, "
    f"execute_method only runs the reviewed read-only methods ({_READ_LIST}); {_GET_NOT_TRUSTED}; "
    f"{_OTHER_READS}."
)
#: 直接寫入關時，不在讀取清單上、也不在白名單的方法（不論上游判成 side_effect、unknown 或依名字判成唯讀的 get_*；
#: 不斷言它會改資料——read_group 這類實際唯讀的方法本來就在清單上）。
DIRECT_OFF_REFUSAL = (
    "Direct writes are off in this deployment (ODOO_MCP_ALLOW_DIRECT_WRITES is off): execute_method runs only "
    f"the reviewed read-only methods ({_READ_LIST}) and the model.method entries an administrator has reviewed "
    f"and allowlisted, so it does not run this method; {_GET_NOT_TRUSTED}. {_OTHER_READS.capitalize()}; use "
    "preview_write -> validate_write -> execute_approved_write for create, write and unlink, and chatter_post "
    "for chatter messages. To run this method directly, an administrator reviews it and adds the exact "
    "'model.method' to ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS or the policy file (ODOO_MCP_POLICY_FILE), or sets "
    "both ODOO_MCP_ALLOW_DIRECT_WRITES=1 and ODOO_MCP_ALLOW_UNKNOWN_METHODS=1."
)
#: chatter 張貼呼叫的範圍（說明與拒絕訊息共用）。
_CHATTER_CALLS = (
    "message_* methods, any call other than a read on mail.message, mail.compose.message and "
    "mail.scheduled.message (create and write included), mail.activity action_done, action_feedback, their "
    "schedule_next variants, action_done_redirect_to_other and action_notify, and activity_feedback, "
    "activity_send_mail, rating_apply and rating_send_request on any model"
)
#: chatter 直接張貼關時的 chatter 張貼呼叫（:func:`is_chatter_call`）。
CHATTER_OFF_REFUSAL = (
    "Direct chatter posting is off in this deployment (MCP_CHATTER_DIRECT is off): execute_method does not "
    f"run calls that post chatter messages ({_CHATTER_CALLS}). Post with chatter_post: its first call returns a "
    "preview with an approval; call it again with the same arguments plus that approval and confirm=true."
)
#: chatter_post 在總閘門關時。
CHATTER_POST_WRITES_DISABLED = (
    "write execution disabled; set ODOO_MCP_ENABLE_WRITES=1 to enable. chatter_post posts nothing while "
    "writes are off."
)


def execute_method_refusal(
    model: str,
    method: str,
    safety: Dict[str, Any],
    is_allowlisted: Callable[[], bool],
    switches: Switches,
) -> Optional[Dict[str, Any]]:
    """``execute_method`` 能不能執行 ``model.method``：可以回 ``None``，否則回上游形狀的拒絕（``success: False``＋``error``）。

    *safety* 是上游 ``classify_method_safety(method)`` 的結果；*is_allowlisted* 只在需要時才呼叫（它每次都重讀政策檔，
    與上游相同：讀取與 create／write／unlink 不讀）。唯讀以 :data:`REVIEWED_READ_METHODS` 為準；上游依名字判成
    ``read_only`` 的其他方法（``get_*``）只在任何方法都直接跑時照上游放行，否則與未審查方法同一套規則。chatter 張貼呼叫
    （:func:`is_chatter_call`）在 create／write／unlink 之前判斷，所以 chatter 關時 mail.message 的 create 也擋。拒絕
    create／write／unlink 時不帶 ``classification``（與上游相同），其他拒絕帶上。
    """
    kind = safety.get("safety")
    if is_reviewed_read(method):
        return None
    if not switches.enable:
        return {"success": False, "error": WRITES_DISABLED_REFUSAL, "classification": _classification(method, safety)}
    if not switches.chatter and is_chatter_call(model, method):
        return {"success": False, "error": CHATTER_OFF_REFUSAL, "classification": _classification(method, safety)}
    if kind == "destructive" or method in STANDARD_WRITES:
        return None if switches.direct else {"success": False, "error": STANDARD_WRITE_REFUSAL}
    if kind == "read_only" and _every_method_runs(switches):
        return None  # 任何方法都直接跑：與上游、第二輪相同（不讀白名單）
    if is_allowlisted():
        return None
    if not switches.direct:
        return {"success": False, "error": DIRECT_OFF_REFUSAL, "classification": _classification(method, safety)}
    if not switches.unknown:
        return {"success": False, "error": UNREVIEWED_REFUSAL, "classification": _classification(method, safety)}
    return None


def chatter_post_refusal(switches: Switches) -> Optional[Dict[str, Any]]:
    """``chatter_post`` 在總閘門關時一律拒絕（不連 Odoo、不回預覽）；其他情況交給上游（直接張貼或預覽＋核准）。"""
    if switches.enable:
        return None
    return {"success": False, "error": CHATTER_POST_WRITES_DISABLED}


# ---- 工具說明（全開時的文字＝第二輪 8c825a5，正式站連接器看到的那份）-----------------------------------

#: 全開（第二輪起的文字，不變）。
METHOD_TEXT_ALL_ON = (
    "Odoo model method to call; any method runs directly in this deployment: create, write "
    "and unlink (ODOO_MCP_ALLOW_DIRECT_WRITES is set; the preview_write -> validate_write -> "
    "execute_approved_write flow is optional) and unreviewed side-effect methods such as "
    "action_confirm or action_post (ODOO_MCP_ALLOW_UNKNOWN_METHODS is set); the Odoo user's "
    "own access rights still apply."
)
_DIRECT_STANDARD = (
    "create, write and unlink run directly in this deployment (ODOO_MCP_ALLOW_DIRECT_WRITES is set; the "
    "preview_write -> validate_write -> execute_approved_write flow is optional)"
)
_CHATTER_OFF_CLAUSE = (
    f"calls that post chatter messages are refused because MCP_CHATTER_DIRECT is off ({_CHATTER_CALLS}): post "
    "messages with chatter_post (its first call returns a preview with an approval; call it again with that "
    "approval and confirm=true); chatter entries that Odoo adds by itself when other calls change records (field "
    "tracking, business methods such as action_confirm) are not covered by this switch"
)
_ACCESS_RIGHTS = "the Odoo user's own access rights still apply."
#: 未審查方法關（直接寫入開）：會直接跑的讀取與被擋的未審查方法（含清單外的 get_*）。
_UNKNOWN_OFF = (
    f"the reviewed read-only methods also run ({_READ_LIST}); unreviewed side-effect methods stay blocked unless "
    f"reviewed and allowlisted (ODOO_MCP_ALLOW_UNKNOWN_METHODS is off), and {_GET_NOT_TRUSTED}; {_OTHER_READS}"
)
#: 直接寫入開、未審查方法關、chatter 直接。第二輪的文字（"...; unreviewed side-effect methods stay blocked unless
#: reviewed and allowlisted; ..."）沒有說哪些讀取會跑；第三輪起清單外的 get_* 也擋，所以列出清單。
METHOD_TEXT_DIRECT_REVIEWED = f"Odoo model method to call; {_DIRECT_STANDARD}; {_UNKNOWN_OFF}; {_ACCESS_RIGHTS}"
METHOD_TEXT_WRITES_DISABLED = (
    "Odoo model method to call; writes are disabled in this deployment (ODOO_MCP_ENABLE_WRITES is off): only "
    f"the reviewed read-only methods run ({_READ_LIST}); create, write, unlink, chatter messages and every other "
    f"method are refused, and {_GET_NOT_TRUSTED}; {_OTHER_READS}."
)


def execute_method_text(switches: Switches) -> str:
    """``execute_method`` 的 ``method`` 參數說明：這個部署裡哪些方法直接跑、哪些被拒、要改走哪個流程。"""
    s = switches
    if not s.enable:
        return METHOD_TEXT_WRITES_DISABLED
    if s.direct:
        if s.chatter:
            return METHOD_TEXT_ALL_ON if s.unknown else METHOD_TEXT_DIRECT_REVIEWED
        if s.unknown:
            allowed = (
                f"{_DIRECT_STANDARD} and so do unreviewed side-effect methods such as action_confirm or "
                "action_post (ODOO_MCP_ALLOW_UNKNOWN_METHODS is set)"
            )
        else:
            allowed = f"{_DIRECT_STANDARD}; {_UNKNOWN_OFF}"
        return f"Odoo model method to call; {allowed}; {_CHATTER_OFF_CLAUSE}; {_ACCESS_RIGHTS}"
    chatter = (
        "post chatter messages with chatter_post, which posts directly (MCP_CHATTER_DIRECT is set)"
        if s.chatter
        else _CHATTER_OFF_CLAUSE
    )
    return (
        "Odoo model method to call; direct writes are off in this deployment (ODOO_MCP_ALLOW_DIRECT_WRITES is off): "
        f"only the reviewed read-only methods run directly ({_READ_LIST}); create, write and unlink are refused here "
        "(use preview_write -> validate_write -> execute_approved_write) and so is every other method (action_*, "
        "button_*, copy, toggle_active, get_* methods that are not on this list and other unreviewed methods) "
        f"unless it is reviewed and allowlisted; {_OTHER_READS}; {chatter}; {_ACCESS_RIGHTS}"
    )


#: chatter 直接（第二輪起的文字，不變）。
CHATTER_TEXT_DIRECT = (
    "Post a chatter message on a mail.thread record. Direct mode is on in this deployment "
    "(MCP_CHATTER_DIRECT): the first call posts the message at once; there is no preview and "
    "no approval token."
)
CHATTER_TEXT_APPROVAL = (
    "Post a chatter message on a mail.thread record. Approval mode is on in this deployment (MCP_CHATTER_DIRECT "
    "is off): the first call only returns a preview with an approval; call chatter_post again with the same "
    "arguments plus that approval and confirm=true to post the message."
)
CHATTER_TEXT_WRITES_DISABLED = (
    "Post a chatter message on a mail.thread record. Writes are disabled in this deployment "
    "(ODOO_MCP_ENABLE_WRITES is off): chatter_post refuses every call and posts nothing."
)


def chatter_post_text(switches: Switches) -> str:
    """``chatter_post`` 的說明。"""
    if not switches.enable:
        return CHATTER_TEXT_WRITES_DISABLED
    return CHATTER_TEXT_DIRECT if switches.chatter else CHATTER_TEXT_APPROVAL


#: 上游 odoo-mcp 1.3.0 的三支審批工具說明（直接寫入開時照用：三階段審批可選用）。
PREVIEW_TEXT = "Preview create, write, or unlink without executing it"
VALIDATE_TEXT = "Validate a standard write payload against optional fields_get metadata"
EXECUTE_APPROVED_TEXT = "Execute a previously previewed and confirmed standard write"
_WRITES_DISABLED = "Writes are disabled in this deployment (ODOO_MCP_ENABLE_WRITES is off)"
_DIRECT_OFF = "Direct writes are off in this deployment (ODOO_MCP_ALLOW_DIRECT_WRITES is off)"


def preview_write_text(switches: Switches) -> str:
    """``preview_write`` 的說明。"""
    if not switches.enable:
        return f"{PREVIEW_TEXT}. {_WRITES_DISABLED}: nothing previewed here can be executed."
    if not switches.direct:
        return (
            f"{PREVIEW_TEXT}. {_DIRECT_OFF}, so this is the first step of the only way to create, write or "
            "unlink records: preview_write -> validate_write -> execute_approved_write."
        )
    return PREVIEW_TEXT


def validate_write_text(switches: Switches) -> str:
    """``validate_write`` 的說明。"""
    if not switches.enable:
        return f"{VALIDATE_TEXT}. {_WRITES_DISABLED}: execute_approved_write refuses every approval returned here."
    if not switches.direct:
        return (
            f"{VALIDATE_TEXT}. {_DIRECT_OFF}: validating against live metadata (the default) returns valid=true "
            "and the approval that execute_approved_write needs (preview_write -> validate_write -> "
            "execute_approved_write)."
        )
    return VALIDATE_TEXT


def execute_approved_write_text(switches: Switches) -> str:
    """``execute_approved_write`` 的說明。"""
    if not switches.enable:
        return f"{EXECUTE_APPROVED_TEXT}. {_WRITES_DISABLED}: every call is refused and nothing is written."
    if not switches.direct:
        return (
            f"{EXECUTE_APPROVED_TEXT}. {_DIRECT_OFF}, so this is the only way to create, write or unlink "
            "records: pass the approval returned by validate_write unchanged, together with confirm=true."
        )
    return EXECUTE_APPROVED_TEXT


# ---- health_check ----------------------------------------------------------------------------------

#: 上游 1.3.0 ``runtime.notes`` 的第 3 句（靜態，全開時是錯的；第三輪 G3 改成 :func:`runtime_note`）。
UPSTREAM_STATIC_NOTE = "execute_method blocks standard destructive methods and unreviewed side-effect methods by default."


def runtime_note(switches: Switches) -> str:
    """``health_check`` 的 ``runtime.notes`` 第 3 句：這個部署的寫入實際怎麼走（跟著四個開關）。

    ``runtime`` 裡的 ``write_execution_enabled``／``direct_writes_enabled``／``unknown_execute_method_enabled``／
    ``chatter_direct_enabled`` 是開關本身的值；總閘門關時其他開關不生效，看這一句。
    """
    s = switches
    if not s.enable:
        return (
            "Writes are disabled (ODOO_MCP_ENABLE_WRITES is off): execute_method runs only the reviewed read-only "
            "methods; chatter_post and execute_approved_write refuse every call."
        )
    if s.direct:
        standard = "execute_method runs create, write and unlink directly (ODOO_MCP_ALLOW_DIRECT_WRITES is set)"
        unreviewed = (
            "unreviewed side-effect methods run directly (ODOO_MCP_ALLOW_UNKNOWN_METHODS is set)"
            if s.unknown
            else "unreviewed side-effect methods, including get_* methods that are not on the reviewed read-only "
            "list, are blocked unless allowlisted (ODOO_MCP_ALLOW_UNKNOWN_METHODS is off)"
        )
    else:
        standard = (
            "execute_method refuses create, write and unlink, which go through preview_write -> validate_write -> "
            "execute_approved_write (ODOO_MCP_ALLOW_DIRECT_WRITES is off)"
        )
        unreviewed = (
            "methods other than the reviewed read-only methods are blocked unless allowlisted (direct writes are off, "
            "so ODOO_MCP_ALLOW_UNKNOWN_METHODS has no effect)"
        )
    chatter = (
        "chatter_post posts directly (MCP_CHATTER_DIRECT is set)"
        if s.chatter
        else "chatter_post posts only after a preview and an approval with confirm=true, and execute_method refuses "
        "calls that post chatter messages (message_* methods, mail.message, mail.compose.message, "
        "mail.scheduled.message, activity feedback, ratings) (MCP_CHATTER_DIRECT is off)"
    )
    return f"{standard}; {unreviewed}; {chatter}."
