"""Odoo 四個寫入開關的 16 種組合：收緊真的有效、說明如實、全開一個位元都不變（第三輪 G1／G2／G3）。

每種組合起一次真的 launcher（修補過的 odoo_mcp、假 Odoo，見 ``_odoo_child``），經 MCP 拿 tools/list 並跑一組固定的
呼叫，以假 Odoo 收到的 ``execute_kw`` 判斷「真的寫了沒」：

- 唯讀＝審過的讀取清單（``write_gates.REVIEWED_READ_METHODS``）：上游依名字當唯讀、其實會寫入的 ``get_portal_url`` 不算，
  收緊之後與未審查方法同一套規則（審查第三輪 G1，:data:`EXTRA_CALLS`）。
- ``ODOO_MCP_ENABLE_WRITES``＝總閘門：關掉時任何工具都不寫（execute_method 只跑審過的讀取方法、chatter_post 拒絕、
  execute_approved_write 拒絕）。
- ``ODOO_MCP_ALLOW_DIRECT_WRITES`` 關：execute_method 不跑 create／write／unlink，也不跑未審查的方法（白名單照跑）。
- ``ODOO_MCP_ALLOW_UNKNOWN_METHODS`` 關：照舊擋未審查方法（上游原文）。
- ``MCP_CHATTER_DIRECT`` 關：chatter_post 先預覽、帶 approval＋confirm 才張貼；execute_method 不跑在 chatter 張貼的呼叫
  （``message_*``、mail.message 的 create、mail.compose.message、完成活動的 action_feedback／activity_feedback…）。
- 說明：五支寫入工具（execute_method 的 method 參數、chatter_post、三支審批工具）如實描述什麼會直接跑、什麼被拒、
  要改走哪個流程（全開＝第二輪的文字）。
- health_check 的 ``runtime.notes`` 第 3 句跟著開關、``broad_unknown_method_mode`` 回實際生效的模式（審查第三輪 G3）；
  validate_write 回明確的 ``valid``（合法 true、不合法 false）。

期望值由 :func:`expected_runs` 照規格直接算（不 import 實作）。全開時 tools/list 與第二輪（8c825a5）逐位元相同
（:data:`ROUND2_ALL_ON_TOOLS_SHA256`），所有呼叫送到 Odoo 的內容也相同（:func:`test_all_on_behaviour_matches_round2`）。
"""

from __future__ import annotations

import hashlib
import itertools
import json

import pytest

pytest.importorskip("odoo_mcp")
pytest.importorskip("odoo_mcp_admin")
pytest.importorskip("httpx")

from _odoo_child import READ_METHODS, SWITCH_KEYS, build_patched_site, payload, run_child  # noqa: E402

#: 全開時 41 支工具的 tools/list（canonical JSON）在第二輪 8c825a5 的 sha256：全開的說明與 schema 不能變。
ROUND2_ALL_ON_TOOLS_SHA256 = "bbcbd988803f95bd720dee3ed22ccbbc533f3b28498db3c0469a2e7719154e99"

#: 白名單（審查過的 model.method；PaaS 只能從容器環境變數或政策檔給）。
ALLOWLIST = ("sale.order.action_confirm", "res.partner.message_notify")

#: execute_method 的呼叫：(model, method, args, kwargs)。
EXECUTE_CALLS = (
    ("res.partner", "create", [{"name": "w"}], {}),
    ("res.partner", "write", [[1], {"name": "w2"}], {}),
    ("res.partner", "unlink", [[1]], {}),
    ("res.partner", "action_archive", [[1]], {}),  # 未審查的副作用方法（action_*）
    ("res.partner", "copy", [1], {}),  # 未知方法（會建立紀錄）
    ("res.partner", "toggle_active", [[1]], {}),  # 未知方法
    ("res.partner", "message_post", [[1]], {"body": "m"}),  # chatter 訊息方法
    ("res.partner", "message_subscribe", [[1]], {"partner_ids": [3]}),  # chatter 訊息方法（未知）
    ("sale.order", "action_confirm", [[1]], {}),  # 白名單裡的副作用方法
    ("res.partner", "message_notify", [[1]], {"body": "n"}),  # 白名單裡的 chatter 訊息方法
    ("res.partner", "search_read", [[]], {"fields": ["name"], "limit": 1}),  # 唯讀
)
#: 審查第三輪 G1 補的呼叫，在第二輪那組之後才跑（全開時第二輪那組送到 Odoo 的內容與順序不變）：
#: 上游依名字當唯讀、其實會寫入的 get_portal_url（Odoo 18 portal.mixin，access_token 空時先寫一個再回分享連結）、
#: 審過的讀取（Odoo 標 @api.readonly）與不在清單上的 default_get。
EXTRA_CALLS = (
    ("account.move", "get_portal_url", [[1]], {}),
    ("sale.order", "get_portal_url", [[1]], {}),
    ("res.partner", "read_group", [[], ["name"], ["name"]], {}),
    ("res.partner", "web_search_read", [[]], {"specification": {"name": {}}, "limit": 1}),
    ("res.partner", "default_get", [["name"]], {}),
    # 不是 message_* 開頭、但在 chatter 張貼的呼叫（MCP_CHATTER_DIRECT 關時要走 chatter_post）
    ("mail.message", "create", [{"model": "res.partner", "res_id": 1, "body": "x", "message_type": "comment"}], {}),
    ("mail.compose.message", "action_send_mail", [[5]], {}),
    ("mail.activity", "action_feedback", [[9]], {"feedback": "done"}),
    ("res.partner", "activity_feedback", [[1]], {"act_type_xmlids": ["mail.mail_activity_data_todo"], "feedback": "f"}),
)
#: 規格裡「在 chatter 張貼的呼叫」（message_* 以外）。
CHATTER_CALLS = {("mail.message", "create"), ("mail.compose.message", "action_send_mail"),
                 ("mail.activity", "action_feedback"), ("res.partner", "activity_feedback")}
#: 審過的讀取方法（規格；完整清單見 apps/odoo/tests/test_write_gates.py）。
REVIEWED_READS = {"search_read", "read_group", "web_search_read"}

COMBOS = [dict(zip("EDUC", bits)) for bits in itertools.product((1, 0), repeat=4)]


def combo_id(c: dict) -> str:
    return "".join(f"{k}{v}" for k, v in c.items())


def combo_env(c: dict) -> dict[str, str]:
    return {key: str(c[letter]) for key, letter in zip(SWITCH_KEYS, "EDUC")}


def expected_runs(model: str, method: str, c: dict) -> bool:
    """規格：execute_method 這個呼叫會不會送到 Odoo。"""
    if method in REVIEWED_READS:
        return True  # 審過的讀取方法永遠可跑（get_portal_url 這種依名字當唯讀的不算）
    if not c["E"]:
        return False  # 總閘門
    if (method.startswith("message_") or (model, method) in CHATTER_CALLS) and not c["C"]:
        return False  # chatter 張貼呼叫走 chatter_post（白名單也不例外；mail.message 的 create 也是）
    if method in ("create", "write", "unlink"):
        return bool(c["D"])
    if f"{model}.{method}" in ALLOWLIST:
        return True  # 審查過的方法
    return bool(c["D"] and c["U"])  # 未審查的方法：要直接寫入＋未審查方法都開


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    return build_patched_site(tmp_path_factory.mktemp("odoo-site"))


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


async def _battery(call_tool, rpc) -> dict:
    out: dict = {"tools": (await rpc("tools/list", {}))["tools"], "execute": []}
    for model, method, args, kwargs in EXECUTE_CALLS:
        out["execute"].append(await call_tool("execute_method", {
            "model": model, "method": method, "args": args, "kwargs": kwargs}))
    first = await call_tool("chatter_post", {"model": "res.partner", "record_id": 1, "body": "c"})
    out["chatter_first"] = first
    out["chatter_second"] = None
    if first.get("isError") is not True and payload(first).get("mode") == "preview":
        out["chatter_second"] = await call_tool("chatter_post", {
            "model": "res.partner", "record_id": 1, "body": "c", "approval": payload(first)["approval"],
            "confirm": True})
    validate = await call_tool("validate_write", {"model": "res.partner", "operation": "create",
                                                  "values": {"name": "v"}})
    out["validate"] = validate
    out["execute_approved"] = await call_tool("execute_approved_write", {
        "approval": payload(validate).get("approval") or {}, "confirm": True})
    out["validate_invalid"] = await call_tool("validate_write", {"model": "res.partner", "operation": "create",
                                                                 "values": {"no_such_field": 1}})
    out["health"] = await call_tool("health_check", {})
    out["extra"] = []
    for model, method, args, kwargs in EXTRA_CALLS:
        out["extra"].append(await call_tool("execute_method", {
            "model": model, "method": method, "args": args, "kwargs": kwargs}))
    return out


def _is_extra(call: dict) -> bool:
    return any(call == {"model": m, "method": n, "args": a, "kwargs": k} for m, n, a, k in EXTRA_CALLS)


def _run(site, tmp_path, combo: dict) -> dict:
    env = {**combo_env(combo), "ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS": ",".join(ALLOWLIST)}
    run = run_child(site, env, tmp_path, _battery)
    out = run["result"]
    out["odoo_calls"] = run["odoo"].calls
    out["odoo_writes"] = run["odoo"].writes()
    out["stderr"] = run["stderr"]
    by_name = {t["name"]: t for t in out["tools"]}
    out["texts"] = {
        "method": by_name["execute_method"]["inputSchema"]["properties"]["method"].get("description"),
        **{name: by_name[name].get("description") for name in
           ("chatter_post", "preview_write", "validate_write", "execute_approved_write")},
    }
    return out


# ---- 全開：與第二輪逐位元相同 ----------------------------------------------------------------------------

ROUND2_METHOD_TEXT = (
    "Odoo model method to call; any method runs directly in this deployment: create, write and unlink "
    "(ODOO_MCP_ALLOW_DIRECT_WRITES is set; the preview_write -> validate_write -> execute_approved_write flow is "
    "optional) and unreviewed side-effect methods such as action_confirm or action_post "
    "(ODOO_MCP_ALLOW_UNKNOWN_METHODS is set); the Odoo user's own access rights still apply."
)
ROUND2_CHATTER_TEXT = (
    "Post a chatter message on a mail.thread record. Direct mode is on in this deployment (MCP_CHATTER_DIRECT): "
    "the first call posts the message at once; there is no preview and no approval token."
)
UPSTREAM_TEXTS = {
    "preview_write": "Preview create, write, or unlink without executing it",
    "validate_write": "Validate a standard write payload against optional fields_get metadata",
    "execute_approved_write": "Execute a previously previewed and confirmed standard write",
}
#: 全開時假 Odoo 收到的寫入（第二輪 8c825a5 實測；chatter_post 直接張貼、validate→execute_approved_write 建立一筆）。
ROUND2_ALL_ON_WRITES = [
    {"model": "res.partner", "method": "create", "args": [{"name": "w"}], "kwargs": {}},
    {"model": "res.partner", "method": "write", "args": [[1], {"name": "w2"}], "kwargs": {}},
    {"model": "res.partner", "method": "unlink", "args": [[1]], "kwargs": {}},
    {"model": "res.partner", "method": "action_archive", "args": [[1]], "kwargs": {}},
    {"model": "res.partner", "method": "copy", "args": [1], "kwargs": {}},
    {"model": "res.partner", "method": "toggle_active", "args": [[1]], "kwargs": {}},
    {"model": "res.partner", "method": "message_post", "args": [[1]], "kwargs": {"body": "m"}},
    {"model": "res.partner", "method": "message_subscribe", "args": [[1]], "kwargs": {"partner_ids": [3]}},
    {"model": "sale.order", "method": "action_confirm", "args": [[1]], "kwargs": {}},
    {"model": "res.partner", "method": "message_notify", "args": [[1]], "kwargs": {"body": "n"}},
    {"model": "res.partner", "method": "message_post", "args": [[1]],
     "kwargs": {"body": "c", "message_type": "comment"}},
    {"model": "res.partner", "method": "create", "args": [{"name": "v"}], "kwargs": {}},
]


def test_all_on_tools_list_is_round2(site, tmp_path):
    """全開：41 支工具的說明與 schema（整份 tools/list）與第二輪 8c825a5 逐位元相同。"""
    out = _run(site, tmp_path, {"E": 1, "D": 1, "U": 1, "C": 1})
    assert len(out["tools"]) == 41
    assert out["texts"]["method"] == ROUND2_METHOD_TEXT
    assert out["texts"]["chatter_post"] == ROUND2_CHATTER_TEXT
    for name, text in UPSTREAM_TEXTS.items():
        assert out["texts"][name] == text
    assert hashlib.sha256(_canonical(out["tools"]).encode()).hexdigest() == ROUND2_ALL_ON_TOOLS_SHA256


def _without_valid(value: dict) -> dict:
    data = payload(value)
    data.pop("valid", None)  # 第三輪 G3 新增的欄位；其他內容不變
    return data


def test_all_on_behaviour_matches_round2(site, tmp_path):
    """全開：每個呼叫都照第二輪送到 Odoo（同樣的 model／method／args／kwargs），回傳也相同。"""
    out = _run(site, tmp_path, {"E": 1, "D": 1, "U": 1, "C": 1})
    writes = [c for c in out["odoo_calls"] if c["method"] not in READ_METHODS and not _is_extra(c)]
    assert writes == ROUND2_ALL_ON_WRITES
    # 第三輪補的呼叫在全開時也都照第二輪直接送到 Odoo（第二輪在全開時任何方法都跑）
    for model, method, args, kwargs in EXTRA_CALLS:
        assert {"model": model, "method": method, "args": args, "kwargs": kwargs} in out["odoo_calls"], method
    assert all(r.get("isError") is not True for r in out["extra"])
    assert [payload(r) for r in out["execute"]] == [
        {"success": True, "result": 42}, {"success": True, "result": True}, {"success": True, "result": True},
        {"success": True, "result": True}, {"success": True, "result": 43}, {"success": True, "result": True},
        {"success": True, "result": 7}, {"success": True, "result": True}, {"success": True, "result": True},
        {"success": True, "result": True}, {"success": True, "result": []},
    ]
    assert all(r.get("isError") is not True for r in out["execute"])
    assert payload(out["chatter_first"]) == {"success": True, "mode": "direct", "model": "res.partner",
                                             "record_id": 1, "approval_required": False, "result": 7}
    assert out["chatter_second"] is None
    validate = _without_valid(out["validate"])
    assert validate["success"] is True and validate["approval_status"]["stored"] is True
    approved = payload(out["execute_approved"])
    assert approved["success"] is True and approved["result"] == 42 and approved["operation"] == "create"


# ---- 16 種組合：收緊真的有效、說明如實 ---------------------------------------------------------------------


@pytest.mark.parametrize("combo", COMBOS, ids=combo_id)
def test_switch_matrix(site, tmp_path, combo):
    out = _run(site, tmp_path, combo)
    c = combo
    texts = out["texts"]

    # execute_method：每個呼叫送不送到 Odoo、被拒時有說明
    for (model, method, _args, _kwargs), result in zip(EXECUTE_CALLS + EXTRA_CALLS, out["execute"] + out["extra"],
                                                       strict=True):
        sent = {"model": model, "method": method, "args": _args, "kwargs": _kwargs} in out["odoo_calls"]
        expected = expected_runs(model, method, c)
        assert sent is expected, (model, method, combo, result)
        assert (result.get("isError") is True) is (not expected), (model, method, result)
        if not expected:
            error = payload(result)["error"]
            if method == "get_portal_url":  # 上游依名字判成唯讀：拒絕時標明不是審過的讀取
                assert payload(result)["classification"]["reviewed_read_only"] is False
            if not c["E"]:
                assert "ODOO_MCP_ENABLE_WRITES" in error
            elif (method.startswith("message_") or (model, method) in CHATTER_CALLS) and not c["C"]:
                assert "MCP_CHATTER_DIRECT is off" in error and "chatter_post" in error
            elif method in ("create", "write", "unlink"):
                assert error.startswith("Direct execute_method blocks create/write/unlink.")
            elif not c["D"]:
                assert "ODOO_MCP_ALLOW_DIRECT_WRITES is off" in error
                assert "may change data" not in error  # 不斷言未知方法會寫入（審查第三輪 G2）
            else:
                assert error.startswith("Unreviewed side-effect methods are blocked by default.")  # 上游原文

    # chatter_post
    chatter_posts = [w for w in out["odoo_calls"] if w["method"] == "message_post" and w["kwargs"].get("body") == "c"]
    first = out["chatter_first"]
    if not c["E"]:
        assert first.get("isError") is True and "ODOO_MCP_ENABLE_WRITES" in payload(first)["error"]
        assert out["chatter_second"] is None and chatter_posts == []
    elif c["C"]:
        assert payload(first)["mode"] == "direct" and len(chatter_posts) == 1
    else:
        assert payload(first)["mode"] == "preview"
        assert payload(out["chatter_second"])["mode"] == "execute" and len(chatter_posts) == 1

    # validate_write 回明確的 valid；execute_approved_write 只在總閘門開時寫
    validate = payload(out["validate"])
    assert validate["valid"] is True and validate["success"] is True
    invalid = payload(out["validate_invalid"])
    assert invalid["valid"] is False and invalid["success"] is False and invalid["approval"] is None
    approved_creates = [w for w in out["odoo_calls"] if w["method"] == "create" and w["args"] == [{"name": "v"}]]
    if c["E"]:
        assert payload(out["execute_approved"])["success"] is True and len(approved_creates) == 1
    else:
        assert out["execute_approved"].get("isError") is True and approved_creates == []
        assert "write execution disabled" in payload(out["execute_approved"])["error"]

    # health_check：broad_unknown_method_mode 是實際生效的模式（審查第三輪 G3），unknown_execute_method_enabled 是開關本身
    runtime = payload(out["health"])["runtime"]
    in_effect = bool(c["E"] and c["D"] and c["U"])
    assert runtime["broad_unknown_method_mode"]["enabled"] is in_effect
    assert runtime["broad_unknown_method_mode"]["risk"] == ("broad" if in_effect else "off")
    assert runtime["unknown_execute_method_enabled"] is bool(c["U"])
    assert runtime["write_execution_enabled"] is bool(c["E"]) and runtime["direct_writes_enabled"] is bool(c["D"])

    # health_check：第 3 句跟著開關
    notes = runtime["notes"]
    assert notes[0] == "HTTP transports are local-only by default in the CLI entry point."
    assert notes[1] == "execute_approved_write requires ODOO_MCP_ENABLE_WRITES and confirm=true."
    note = notes[2]
    assert note != "execute_method blocks standard destructive methods and unreviewed side-effect methods by default."
    if not c["E"]:
        assert note.startswith("Writes are disabled (ODOO_MCP_ENABLE_WRITES is off)")
    else:
        assert ("runs create, write and unlink directly" in note) is bool(c["D"])
        assert ("unreviewed side-effect methods run directly" in note) is bool(c["D"] and c["U"])
        assert ("chatter_post posts directly" in note) is bool(c["C"])

    # 說明：如實描述能做什麼、要走什麼流程
    if not (c["E"] and c["D"] and c["U"]):  # 收緊之後列出會跑的讀取與清單外讀取的替代做法（審查第三輪 G2）
        assert "for other reads use search_records, read_record or aggregate_records" in texts["method"]
        assert "get_* methods that are not on this list" in texts["method"]
        assert "read_group" in texts["method"] and "web_search_read" in texts["method"]
    if not c["E"]:
        assert "only the reviewed read-only methods run" in texts["method"] and "ODOO_MCP_ENABLE_WRITES is off" in texts["method"]
        assert "ODOO_MCP_ENABLE_WRITES is off" in texts["chatter_post"]
        for name in UPSTREAM_TEXTS:
            assert texts[name].startswith(UPSTREAM_TEXTS[name] + ". ") and "ODOO_MCP_ENABLE_WRITES is off" in texts[name]
    else:
        if c["D"]:
            assert "ODOO_MCP_ALLOW_DIRECT_WRITES is set" in texts["method"]
            assert "execute_approved_write flow is optional" in texts["method"]
            assert ("ODOO_MCP_ALLOW_UNKNOWN_METHODS is set" in texts["method"]) is bool(c["U"])
            assert ("stay blocked unless reviewed and allowlisted" in texts["method"]) is not bool(c["U"])
            for name, text in UPSTREAM_TEXTS.items():
                assert texts[name] == text  # 三階段審批可選用：上游原文
        else:
            assert "ODOO_MCP_ALLOW_DIRECT_WRITES is off" in texts["method"]
            assert "preview_write -> validate_write -> execute_approved_write" in texts["method"]
            for name in UPSTREAM_TEXTS:
                assert texts[name].startswith(UPSTREAM_TEXTS[name] + ". ")
                assert "ODOO_MCP_ALLOW_DIRECT_WRITES is off" in texts[name]
        if c["C"]:
            assert texts["chatter_post"] == ROUND2_CHATTER_TEXT
            assert "MCP_CHATTER_DIRECT is off" not in texts["method"]
        else:
            assert "MCP_CHATTER_DIRECT is off" in texts["chatter_post"] and "confirm=true" in texts["chatter_post"]
            assert "MCP_CHATTER_DIRECT is off" in texts["method"] and "chatter_post" in texts["method"]
    if all(c.values()):
        assert texts["method"] == ROUND2_METHOD_TEXT
