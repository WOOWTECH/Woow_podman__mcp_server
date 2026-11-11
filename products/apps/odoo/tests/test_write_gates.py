"""``odoo_mcp_admin.write_gates``：四個寫入開關的決定與說明（純函式；真的子行程見 core 的 test_odoo_write_gates.py）。"""

from __future__ import annotations

import ast
import importlib.util
import itertools
from pathlib import Path

import pytest

from odoo_mcp import diagnostics
from odoo_mcp_admin import write_gates as wg
from odoo_mcp_admin.write_gates import Switches

ALL = [Switches(*bits) for bits in itertools.product((True, False), repeat=4)]
ALL_ON = Switches(True, True, True, True)

#: execute_method 的呼叫 (model, method)；分類用上游 odoo-mcp 1.3.0 真的 classify_method_safety（修補後的 guard 也是）。
CALLS = (
    ("res.partner", "search_read"), ("res.partner", "read"), ("res.partner", "get_views"),
    ("res.partner", "read_group"), ("res.partner", "web_read"),  # 審過的讀取（Odoo 標 @api.readonly），上游判成 unknown
    ("sale.order", "get_portal_url"),  # Odoo 18 portal.mixin：access_token 空時先寫一個再回分享連結（上游依名字判成唯讀）
    ("res.partner", "get_wallet_loyalty_card"),  # WoowTech sh_subscription_wallet：找不到卡就 create
    ("res.partner", "default_get"),  # 預設值可以是任何程式（例如取序號），不在讀取清單
    ("res.partner", "create"), ("res.partner", "write"), ("res.partner", "unlink"),
    ("res.partner", "action_archive"), ("stock.picking", "button_validate"), ("res.partner", "message_post"),
    ("res.partner", "message_post_with_source"),
    ("res.partner", "copy"), ("res.partner", "toggle_active"), ("res.partner", "name_create"),
    ("res.partner", "message_subscribe"), ("res.partner", "message_notify"),
    # 不是 message_* 開頭、但在 chatter 張貼的呼叫（審查第三輪 G1 的 chatter 部分；對照 odoo/odoo 18.0 mail、rating）
    ("mail.message", "create"), ("mail.message", "write"), ("mail.compose.message", "create"),
    ("mail.compose.message", "action_send_mail"), ("mail.scheduled.message", "post_message"),
    ("mail.activity", "action_feedback"), ("mail.activity", "action_done"), ("mail.activity", "action_notify"),
    ("res.partner", "activity_feedback"), ("res.partner", "activity_send_mail"), ("res.partner", "rating_apply"),
    ("mail.message", "search_read"),  # 訊息 model 上的讀取照跑
    ("res.partner", "action_done"),  # 不是 mail.activity 的同名方法：不算 chatter 張貼
    ("mail.activity", "write"),  # 改期、改指派：不張貼（指派通知走 action_notify）
)
#: 規格裡「審過的讀取方法」（CALLS 裡的那幾個；完整清單由 test_reviewed_read_methods_are_pinned 釘住）。
REVIEWED = {"search_read", "read", "get_views", "read_group", "web_read"}
#: 規格裡「在 chatter 張貼的呼叫」（message_* 以外；與 write_gates 分開寫一次）。
CHATTER_SPEC = {
    ("mail.message", "create"), ("mail.message", "write"), ("mail.compose.message", "create"),
    ("mail.compose.message", "action_send_mail"), ("mail.scheduled.message", "post_message"),
    ("mail.activity", "action_feedback"), ("mail.activity", "action_done"), ("mail.activity", "action_notify"),
    ("res.partner", "activity_feedback"), ("res.partner", "activity_send_mail"), ("res.partner", "rating_apply"),
}


def safety(method: str) -> dict:
    return diagnostics.classify_method_safety(method)


def spec(model: str, method: str, allowlisted: bool, s: Switches) -> bool:
    """規格：execute_method 會不會執行（與 write_gates 的 docstring 同一份規則，獨立寫一次）。"""
    if method in REVIEWED:
        return True  # 審過的讀取方法永遠可跑
    if not s.enable:
        return False
    if (method.startswith("message_") or (model, method) in CHATTER_SPEC) and not s.chatter:
        return False  # chatter 張貼呼叫走 chatter_post（白名單也不例外；mail.message 的 create 也是）
    if method in ("create", "write", "unlink"):
        return s.direct
    if allowlisted:
        return True
    return s.direct and s.unknown  # 未審查方法（含不在讀取清單上的 get_*）：要直接寫入＋未審查方法都開


@pytest.mark.parametrize("switches", ALL, ids=lambda s: "E{:d}D{:d}U{:d}C{:d}".format(*vars(s).values()))
@pytest.mark.parametrize("allowlisted", [False, True])
def test_execute_method_decision_follows_the_rules(switches, allowlisted):
    for model, method in CALLS:
        refusal = wg.execute_method_refusal(model, method, safety(method), lambda: allowlisted, switches)
        assert (refusal is None) is spec(model, method, allowlisted, switches), (model, method, switches, allowlisted)
        if refusal is not None:
            assert refusal["success"] is False and refusal["error"]
            assert ("classification" in refusal) is (refusal["error"] != wg.STANDARD_WRITE_REFUSAL)


def test_all_on_runs_everything_like_round_two():
    for model, method in CALLS:
        assert wg.execute_method_refusal(model, method, safety(method), lambda: False, ALL_ON) is None


def test_allowlist_is_read_only_when_it_decides():
    """上游每次呼叫都重讀政策檔：唯讀、create／write／unlink、總閘門關與 chatter 規則先決定時都不讀。"""
    calls = []

    def allowlisted():
        calls.append(1)
        return False

    for method in ("search_read", "read_group", "get_portal_url", "create", "write", "unlink"):
        wg.execute_method_refusal("res.partner", method, safety(method), allowlisted, ALL_ON)
    wg.execute_method_refusal("res.partner", "action_archive", {"safety": "side_effect"}, allowlisted,
                              Switches(False, True, True, True))
    wg.execute_method_refusal("res.partner", "get_portal_url", {"safety": "read_only"}, allowlisted,
                              Switches(False, True, True, True))
    wg.execute_method_refusal("res.partner", "message_post", {"safety": "side_effect"}, allowlisted,
                              Switches(True, True, True, False))
    wg.execute_method_refusal("mail.activity", "action_feedback", {"safety": "side_effect"}, allowlisted,
                              Switches(True, True, True, False))
    wg.execute_method_refusal("res.partner", "read_group", {"safety": "unknown"}, allowlisted,
                              Switches(True, False, False, False))
    assert calls == []  # 全開時 get_* 照上游當讀取、不讀白名單；審過的讀取在任何組合都不讀；chatter 規則先決定
    wg.execute_method_refusal("res.partner", "action_archive", {"safety": "side_effect"}, allowlisted, ALL_ON)
    assert calls == [1]  # 全開時與上游相同：未審查方法會查一次白名單
    wg.execute_method_refusal("res.partner", "get_portal_url", {"safety": "read_only"}, allowlisted,
                              Switches(True, False, True, True))
    assert calls == [1, 1]  # 直接寫入關：不在讀取清單上的 get_* 要看白名單


def test_reviewed_read_methods_are_pinned():
    """審過的讀取清單＝上游 READ_ONLY_METHODS 的精確名單＋Odoo 標 @api.readonly 的讀取 API；說明列的名字與清單相同。"""
    from odoo_mcp import diagnostics

    assert set(wg.UPSTREAM_READ_ONLY_METHODS) == set(diagnostics.READ_ONLY_METHODS)
    assert set(wg.ODOO_READONLY_API_METHODS) == {
        "search_fetch", "read_group", "get_views", "web_read", "web_search_read", "web_read_group",
        "web_name_search", "read_progress_bar", "formatted_read_group", "formatted_read_grouping_sets"}
    assert wg.REVIEWED_READ_METHODS == set(diagnostics.READ_ONLY_METHODS) | set(wg.ODOO_READONLY_API_METHODS)
    listed = set(wg._READ_LIST.replace(" and the web client reads ", ", ").replace(" and ", ", ").split(", "))
    assert listed == wg.REVIEWED_READ_METHODS
    # 上游依名字把 get_* 判成唯讀；寫入的 get_portal_url 也是（所以不能照上游的分類放行）
    assert diagnostics.classify_method_safety("get_portal_url")["safety"] == "read_only"
    assert "get_portal_url" not in wg.REVIEWED_READ_METHODS and "default_get" not in wg.REVIEWED_READ_METHODS


@pytest.mark.parametrize("switches", ALL, ids=lambda s: "E{:d}D{:d}U{:d}C{:d}".format(*vars(s).values()))
def test_get_methods_off_the_list_are_unreviewed_methods(switches):
    """get_portal_url（會寫入 access_token）：只有任何方法都直接跑時才跑；拒絕時說明是哪個開關、分類標明不是審過的讀取。"""
    s = switches
    refusal = wg.execute_method_refusal("sale.order", "get_portal_url", {"safety": "read_only", "confidence": "medium"},
                                        lambda: False, s)
    if s.enable and s.direct and s.unknown:
        assert refusal is None
        return
    assert refusal["success"] is False
    assert refusal["classification"] == {"safety": "read_only", "confidence": "medium", "reviewed_read_only": False}
    if not s.enable:
        assert refusal["error"] == wg.WRITES_DISABLED_REFUSAL
    elif not s.direct:
        assert refusal["error"] == wg.DIRECT_OFF_REFUSAL
    else:
        assert refusal["error"] == wg.UNREVIEWED_REFUSAL  # 上游原文
    # 白名單照常有效（總閘門開時）
    allowed = wg.execute_method_refusal("sale.order", "get_portal_url", {"safety": "read_only"}, lambda: True, s)
    assert (allowed is None) is s.enable


def test_writes_disabled_texts_list_the_reviewed_reads():
    off = Switches(False, True, True, True)
    for text in (wg.execute_method_text(off), wg.WRITES_DISABLED_REFUSAL):
        assert "get_* methods that are not on this list are not treated as reads" in text
        assert "get_portal_url" in text
        for name in wg.REVIEWED_READ_METHODS:
            assert name in text, name
        assert "and get_* methods)" not in text  # 第三輪 e228ee1 的「get_* 都是唯讀」說法


@pytest.mark.parametrize("switches", ALL, ids=lambda s: "E{:d}D{:d}U{:d}C{:d}".format(*vars(s).values()))
def test_refusal_messages_name_the_switch_that_refused(switches):
    s = switches
    create = wg.execute_method_refusal("res.partner", "create", {"safety": "destructive"}, lambda: False, s)
    archive = wg.execute_method_refusal("res.partner", "action_archive", {"safety": "side_effect"}, lambda: False, s)
    message = wg.execute_method_refusal("res.partner", "message_post", {"safety": "side_effect"}, lambda: False, s)
    mail_create = wg.execute_method_refusal("mail.message", "create", {"safety": "destructive"}, lambda: False, s)
    feedback = wg.execute_method_refusal("mail.activity", "action_feedback", {"safety": "side_effect"}, lambda: True, s)
    if not s.enable:
        for refusal in (create, archive, message, mail_create, feedback):
            assert refusal["error"] == wg.WRITES_DISABLED_REFUSAL
        assert wg.chatter_post_refusal(s) == {"success": False, "error": wg.CHATTER_POST_WRITES_DISABLED}
        return
    assert wg.chatter_post_refusal(s) is None
    assert (create is None) is s.direct
    if create is not None:
        assert create == {"success": False, "error": wg.STANDARD_WRITE_REFUSAL}
    if not s.chatter:
        # chatter 張貼呼叫：mail.message 的 create 在直接寫入開時也擋、白名單裡的 action_feedback 也擋
        for refusal in (message, mail_create, feedback):
            assert refusal["error"] == wg.CHATTER_OFF_REFUSAL
    else:
        assert feedback is None  # 白名單
        assert (mail_create is None) is s.direct
    if not s.direct:
        assert archive["error"] == wg.DIRECT_OFF_REFUSAL
    elif not s.unknown:
        assert archive["error"] == wg.UNREVIEWED_REFUSAL
    else:
        assert archive is None


# ---- 說明 ------------------------------------------------------------------------------------------------

ROUND2_METHOD_ALL_ON = (
    "Odoo model method to call; any method runs directly in this deployment: create, write and unlink "
    "(ODOO_MCP_ALLOW_DIRECT_WRITES is set; the preview_write -> validate_write -> execute_approved_write flow is "
    "optional) and unreviewed side-effect methods such as action_confirm or action_post "
    "(ODOO_MCP_ALLOW_UNKNOWN_METHODS is set); the Odoo user's own access rights still apply."
)
#: 第二輪 E1D1U0C1 的文字：沒有說哪些讀取會跑。第三輪起清單外的 get_* 也擋（審查第三輪 G1），所以改成列出清單（G2）。
ROUND2_METHOD_DIRECT_REVIEWED = (
    "Odoo model method to call; create, write and unlink run directly in this deployment "
    "(ODOO_MCP_ALLOW_DIRECT_WRITES is set; the preview_write -> validate_write -> execute_approved_write flow is "
    "optional); unreviewed side-effect methods stay blocked unless reviewed and allowlisted; the Odoo user's own "
    "access rights still apply."
)
ROUND2_CHATTER_DIRECT = (
    "Post a chatter message on a mail.thread record. Direct mode is on in this deployment (MCP_CHATTER_DIRECT): "
    "the first call posts the message at once; there is no preview and no approval token."
)
ROUND2_STANDARD_WRITE_REFUSAL = (
    "Direct execute_method blocks create/write/unlink. Use preview_write -> validate_write -> "
    "execute_approved_write, or set ODOO_MCP_ALLOW_DIRECT_WRITES=1."
)


def _upstream_constants() -> list[str]:
    """上游（venv 裡未修補的）odoo-mcp 1.3.0 ``tools_write.py`` 的字串常數（相鄰字串在解析時已接成一個）。"""
    origin = Path(importlib.util.find_spec("odoo_mcp").origin).parent / "tools_write.py"
    tree = ast.parse(origin.read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_unchanged_texts_are_round_two_and_upstream_verbatim():
    assert wg.METHOD_TEXT_ALL_ON == ROUND2_METHOD_ALL_ON
    assert wg.CHATTER_TEXT_DIRECT == ROUND2_CHATTER_DIRECT
    assert wg.STANDARD_WRITE_REFUSAL == ROUND2_STANDARD_WRITE_REFUSAL
    upstream = _upstream_constants()
    for text in (wg.PREVIEW_TEXT, wg.VALIDATE_TEXT, wg.EXECUTE_APPROVED_TEXT, wg.UNREVIEWED_REFUSAL):
        assert text in upstream, text
    assert wg.UPSTREAM_STATIC_NOTE in Path(importlib.util.find_spec("odoo_mcp").origin).parent.joinpath(
        "server_core.py").read_text(encoding="utf-8")


def test_all_on_texts_are_round_two():
    assert wg.execute_method_text(ALL_ON) == ROUND2_METHOD_ALL_ON
    assert wg.chatter_post_text(ALL_ON) == ROUND2_CHATTER_DIRECT
    assert wg.preview_write_text(ALL_ON) == wg.PREVIEW_TEXT
    assert wg.validate_write_text(ALL_ON) == wg.VALIDATE_TEXT
    assert wg.execute_approved_write_text(ALL_ON) == wg.EXECUTE_APPROVED_TEXT
    reviewed = wg.execute_method_text(Switches(True, True, False, True))
    assert reviewed == wg.METHOD_TEXT_DIRECT_REVIEWED != ROUND2_METHOD_DIRECT_REVIEWED  # 第三輪起列出讀取清單
    assert reviewed.startswith(ROUND2_METHOD_DIRECT_REVIEWED.split("; unreviewed")[0] + "; ")


#: 收緊之後（不是任何方法都直接跑）每一句 method 說明與拒絕訊息都要列出會跑的讀取、指出清單外讀取的替代做法（審查第三輪 G2）。
ALTERNATIVES = "for other reads use search_records, read_record or aggregate_records"


@pytest.mark.parametrize("switches", ALL, ids=lambda s: "E{:d}D{:d}U{:d}C{:d}".format(*vars(s).values()))
def test_tightened_texts_list_the_reads_and_the_alternatives(switches):
    s = switches
    method = wg.execute_method_text(s)
    if s.enable and s.direct and s.unknown:
        assert "reviewed read-only" not in method  # 任何方法都直接跑：讀取清單不影響行為
        return
    for name in wg.REVIEWED_READ_METHODS:
        assert name in method, (name, s)
    assert ALTERNATIVES in method and "get_* methods that are not on this list" in method
    assert "may change data" not in method


def test_refusals_list_the_reads_and_do_not_claim_the_method_writes():
    """直接寫入關時拒絕 read_group 以外的未知方法（例如 default_get）：不斷言它會改資料，列出會跑的讀取與替代做法。"""
    for refusal in (wg.DIRECT_OFF_REFUSAL, wg.WRITES_DISABLED_REFUSAL):
        assert "may change data" not in refusal
        assert ALTERNATIVES in refusal.lower()
        for name in wg.REVIEWED_READ_METHODS:
            assert name in refusal, name
    got = wg.execute_method_refusal("res.partner", "default_get", {"safety": "unknown"}, lambda: False,
                                    Switches(True, False, True, True))
    assert got["error"] == wg.DIRECT_OFF_REFUSAL and got["classification"] == {"safety": "unknown"}
    assert wg.execute_method_refusal("res.partner", "read_group", {"safety": "unknown"}, lambda: False,
                                     Switches(True, False, True, True)) is None


@pytest.mark.parametrize("switches", ALL, ids=lambda s: "E{:d}D{:d}U{:d}C{:d}".format(*vars(s).values()))
def test_texts_say_what_runs_and_which_flow_to_use(switches):
    s = switches
    method = wg.execute_method_text(s)
    chatter = wg.chatter_post_text(s)
    approval = (wg.preview_write_text(s), wg.validate_write_text(s), wg.execute_approved_write_text(s))
    note = wg.runtime_note(s)
    assert method.startswith("Odoo model method to call; ")
    if not s.enable:
        assert "ODOO_MCP_ENABLE_WRITES is off" in method and "only the reviewed read-only methods run" in method
        assert "refuses every call" in chatter
        assert all("ODOO_MCP_ENABLE_WRITES is off" in text for text in approval)
        assert note.startswith("Writes are disabled (ODOO_MCP_ENABLE_WRITES is off)")
        return
    # 審核寫入開：直接寫入決定 create／write／unlink 要不要走三階段
    if s.direct:
        assert "ODOO_MCP_ALLOW_DIRECT_WRITES is set" in method and "flow is optional" in method
        assert approval == (wg.PREVIEW_TEXT, wg.VALIDATE_TEXT, wg.EXECUTE_APPROVED_TEXT)
        assert ("ODOO_MCP_ALLOW_UNKNOWN_METHODS is set" in method) is s.unknown
    else:
        assert "ODOO_MCP_ALLOW_DIRECT_WRITES is off" in method
        assert "preview_write -> validate_write -> execute_approved_write" in method
        assert "ODOO_MCP_ALLOW_UNKNOWN_METHODS" not in method  # 直接寫入關時未審查方法開關沒有作用
        assert all("ODOO_MCP_ALLOW_DIRECT_WRITES is off" in text for text in approval)
    if s.chatter:
        assert chatter == ROUND2_CHATTER_DIRECT and "MCP_CHATTER_DIRECT is off" not in method
    else:
        assert "MCP_CHATTER_DIRECT is off" in chatter and "confirm=true" in chatter
        assert "MCP_CHATTER_DIRECT is off" in method and "chatter_post" in method
    assert ("runs create, write and unlink directly" in note) is s.direct
    assert ("unreviewed side-effect methods run directly" in note) is (s.direct and s.unknown)
    assert ("chatter_post posts directly" in note) is s.chatter
    assert note != wg.UPSTREAM_STATIC_NOTE


def test_every_combination_has_its_own_method_text_where_behaviour_differs():
    """說明是行為的函式：行為一樣的組合（直接寫入關時的未審查方法開關、總閘門關時其他三個）才可以同一句。"""
    def behaviour(s):
        return tuple(wg.execute_method_refusal(model, method, safety(method), lambda: False, s) is None
                     for model, method in CALLS)

    seen = {}
    for s in ALL:
        seen.setdefault(wg.execute_method_text(s), set()).add(behaviour(s))
    assert all(len(behaviours) == 1 for behaviours in seen.values())
    assert len(seen) == 7  # 全開、U0、C0×(U1/U0)、D0×(C1/C0)、總閘門關


@pytest.mark.parametrize("switches", ALL, ids=lambda s: "E{:d}D{:d}U{:d}C{:d}".format(*vars(s).values()))
def test_unknown_methods_run_only_when_writes_direct_and_unknown_are_on(switches):
    """health_check 的 broad_unknown_method_mode：只開未審查方法、但總閘門或直接寫入關時沒有作用（審查第三輪 G3）。"""
    s = switches
    assert wg.unknown_methods_run(s) is (s.enable and s.direct and s.unknown)
    archive = wg.execute_method_refusal("res.partner", "action_archive", {"safety": "side_effect"}, lambda: False, s)
    assert (archive is None) is wg.unknown_methods_run(s)  # 與決定是同一個判斷


def test_is_chatter_method():
    assert wg.is_chatter_method("message_post") and wg.is_chatter_method("message_subscribe")
    assert not wg.is_chatter_method("action_post") and not wg.is_chatter_method("post_message")


def test_chatter_calls_cover_the_posting_paths_and_only_those():
    """MCP_CHATTER_DIRECT 關時要改走 chatter_post 的呼叫：message_*、訊息 model 上讀取以外的呼叫、完成活動與評分的張貼方法。"""
    posting = [
        ("res.partner", "message_post"), ("discuss.channel", "message_post"), ("res.partner", "message_notify"),
        ("mail.message", "create"), ("mail.message", "write"), ("mail.message", "copy"), ("mail.message", "unlink"),
        ("mail.compose.message", "create"), ("mail.compose.message", "action_send_mail"),
        ("mail.compose.message", "action_schedule_message"), ("mail.scheduled.message", "create"),
        ("mail.scheduled.message", "post_message"),
        ("mail.activity", "action_done"), ("mail.activity", "action_feedback"), ("mail.activity", "action_done_schedule_next"),
        ("mail.activity", "action_feedback_schedule_next"), ("mail.activity", "action_done_redirect_to_other"),
        ("mail.activity", "action_notify"),
        ("res.partner", "activity_feedback"), ("crm.lead", "activity_send_mail"), ("project.task", "rating_apply"),
        ("project.task", "rating_send_request"),
    ]
    not_posting = [
        ("mail.message", "search_read"), ("mail.message", "read"), ("mail.compose.message", "fields_get"),
        ("mail.activity", "write"), ("mail.activity", "unlink"), ("mail.activity", "action_snooze"),
        ("res.partner", "activity_schedule"), ("res.partner", "action_done"), ("res.partner", "write"),
        ("sale.order", "action_confirm"), ("mail.followers", "create"), ("res.partner", "post_message"),
    ]
    for model, method in posting:
        assert wg.is_chatter_call(model, method), (model, method)
    for model, method in not_posting:
        assert not wg.is_chatter_call(model, method), (model, method)


def test_chatter_off_texts_name_the_posting_paths_and_the_boundary():
    off = Switches(True, True, True, False)
    method = wg.execute_method_text(off)
    for text in (method, wg.CHATTER_OFF_REFUSAL):
        for name in ("message_*", "mail.message", "mail.compose.message", "mail.scheduled.message", "action_feedback",
                     "activity_feedback", "activity_send_mail", "rating_apply", "chatter_post", "confirm=true"):
            assert name in text, (name, text)
    # 界線寫清楚：其他呼叫改資料時 Odoo 自己在 chatter 留的紀錄不歸這個開關
    assert "are not covered by this switch" in method and "field tracking" in method
    assert "calls that post chatter messages" in wg.runtime_note(off)
