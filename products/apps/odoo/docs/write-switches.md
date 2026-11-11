# Odoo MCP 的四個寫入開關（語意、說明與驗證）

負責人 2026-10-25 決定「預設全開」：新安裝四個開關都開，既有設定第一次以新映像啟動時一次性改成全開（`owner_defaults`）。
之後管理員在 Settings 頁自己關掉的開關要**保留而且真的有效**——第三輪（2026-10-27）把這件事補齊：第二輪的沙盒證實
`ODOO_MCP_ALLOW_UNKNOWN_METHODS=1` 時，關掉 Allow Direct Writes 擋不住 `action_archive` 這類未審查方法，也擋不住經
`execute_method` 呼叫 `message_post` 繞過 Direct Chatter Post；標成「寫入總閘門」的 Enable Approved Writes 也只擋
`execute_approved_write`。

## 語意

| 開關（Settings 頁） | 環境變數 | 關掉時 |
|---|---|---|
| Enable Approved Writes（寫入總閘門） | `ODOO_MCP_ENABLE_WRITES` | **任何工具都不寫**：`execute_method` 只跑審過的讀取方法（見下），其他一律拒絕（含 create／write／unlink、未審查方法、不在讀取清單上的 `get_*`、白名單裡的方法、chatter 訊息方法）；`chatter_post` 第一次呼叫就拒絕（不回預覽）；`execute_approved_write` 拒絕。`preview_write`／`validate_write` 不寫入，照常回結果，但說明寫明拿到的核准無法執行。 |
| Allow Direct Writes | `ODOO_MCP_ALLOW_DIRECT_WRITES` | `execute_method` 只直接執行審過的讀取方法與白名單：create／write／unlink 要走 `preview_write` → `validate_write` → `execute_approved_write`；未審查的副作用／未知方法（action_*、button_*、copy、toggle_active、不在讀取清單上的 `get_*`…）拒絕並說明（此時 Allow Unreviewed Methods 沒有作用）；只有審查過、列入白名單的 `model.method` 照跑。 |
| Allow Unreviewed Methods | `ODOO_MCP_ALLOW_UNKNOWN_METHODS` | 照上游：未審查的副作用／未知方法擋下（白名單照跑），錯誤訊息與上游 1.3.0 逐字相同；不在讀取清單上的 `get_*` 也算未審查方法（上游依名字放行）。 |
| Direct Chatter Post | `MCP_CHATTER_DIRECT` | `chatter_post` 先回預覽與 approval，帶 approval 與 `confirm=true` 再呼叫一次才張貼；`execute_method` 也不跑**在 chatter 張貼訊息的呼叫**（見下，白名單也不例外），錯誤訊息指向 `chatter_post`。 |

「唯讀方法」＝**審過的讀取清單**（`write_gates.REVIEWED_READ_METHODS`）：

- 上游 odoo-mcp 1.3.0 `READ_ONLY_METHODS` 的精確名單：search、search_count、search_read、read、fields_get、name_get、
  name_search、context_get；
- Odoo 自己標 `@api.readonly`（可以在資料庫唯讀副本上跑、本來就不能寫）的讀取 API：search_fetch、read_group
  （`odoo/models.py`）、get_views（`base/models/ir_ui_view.py`）、web_read、web_search_read、web_read_group、
  read_progress_bar（`web/models/models.py`，以上 18.0），19.0 的 web 另有 web_name_search、formatted_read_group、
  formatted_read_grouping_sets。

上游 `classify_method_safety` 另外依名字把所有 `get_*`／`_get_*` 判成 `read_only`，但名字不保證不寫：Odoo 18 標準的
`portal.mixin.get_portal_url`（sale.order、account.move、purchase.order、project.task…都有，沒有 `@api.private`）在
access_token 空的時候先 `sudo().write({'access_token': …})` 再回公開分享連結；WoowTech 自己的 addon 也有
（`sh_subscription_wallet` 的 `get_wallet_loyalty_card` 找不到卡就建立 loyalty.card，`ecpay_invoice_tw` 的
`get_ecpay_invoice_info` 打綠界 API 後寫回）。所以**不在清單上的方法——包括這些 `get_*`——收緊之後一律算「未審查方法」**；
只有直接寫入與未審查方法都開（任何方法都直接跑）時，`get_*` 才照上游當讀取直接跑。`default_get` 也不在清單：欄位預設值
可以是任何程式（例如取 ir.sequence 的下一號會推進序號）。
其他方法上游分成 destructive（create／write／unlink）、side_effect（action_*、button_*、名字含 send／post／validate、
message_post）與 unknown（其餘，例如 copy、toggle_active、name_create、default_get）——unknown 也可能寫入，所以與
side_effect 同樣算「未審查方法」。收緊之後不在清單上的讀取改用專用的唯讀工具（`search_records`、`read_record`、
`aggregate_records`…），或把該 `model.method` 列入白名單（白名單只在總閘門開時生效）。
白名單＝`ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS`（逗號分隔的 `model.method`）或政策檔（`ODOO_MCP_POLICY_FILE`）；
PaaS 可在 Settings → Reviewed Odoo methods 編輯精確清單，每行一項，儲存後重新啟動子程序。
API 為 `GET /api/odoo/allowed-methods` 與帶 `If-Match` 的 `PUT /api/odoo/allowed-methods`，
body 是 `{"odoo_allowed_methods":["sale.order.action_confirm"]}`；欄位存於 `mcp_server.odoo_allowed_methods`。
最多 128 個不重複的公開方法、每項 256 字元、compact JSON 總計 16 KiB；拒絕空白、萬用字元與私有方法。
未儲存時不改舊設定，仍繼承容器環境的清單；儲存後（包括空清單）覆寫該環境值。政策檔仍由上游另外合併，
表單不編輯政策檔；`health_check.runtime.allowed_side_effect_methods` 顯示合併結果。
清單不授予工具權限，也不繞過寫入總閘門、chatter、直接 create／write／unlink 或 Odoo ACL 限制；
直接寫入與未審查方法都開時，清單不會限制其他方法。

「在 chatter 張貼訊息的呼叫」（`write_gates.is_chatter_call`；對照 odoo/odoo 18.0 的 mail、rating 原始碼）：

- mail.thread 的 `message_*` 方法（message_post、message_post_with_source、message_notify、message_subscribe…，任何 model）；
- 訊息本身的 model 上任何讀取以外的呼叫：`mail.message`、chatter 撰寫器 `mail.compose.message`、排程訊息
  `mail.scheduled.message`（create、write、copy、action_send_mail、action_schedule_message、post_message…都算）；
- 完成活動並把結果貼上 chatter：`mail.activity` 的 action_done、action_feedback、action_done_schedule_next、
  action_feedback_schedule_next、action_done_redirect_to_other（都經 `_action_done` 貼「已完成」訊息）與指派通知
  action_notify（經 message_notify）；任何 model 的 activity_feedback（mail.activity.mixin）、activity_send_mail（以範本
  message_post_with_source）；
- 評分：rating_apply（貼評分與回饋）、rating_send_request（以範本張貼評分邀請）。

**不在這個開關範圍**：其他呼叫改資料時 Odoo 自己在 chatter 留的紀錄——欄位追蹤（tracking）、action_confirm／action_post
這類業務方法順帶貼的訊息、建立或改派活動時的指派通知（mail.activity 的 create／write 內部呼叫 action_notify）。這些由直接
寫入與未審查方法兩個開關管：要它們也先經核准，就關掉 Allow Direct Writes（或 Allow Unreviewed Methods）。說明與拒絕訊息
都寫出這條界線。

`execute_method` 的判斷順序（`odoo_mcp_admin/write_gates.py` 的 `execute_method_refusal(model, method, …)`）：

1. 審過的讀取方法 → 執行；
2. 總閘門關 → 拒絕；
3. chatter 張貼呼叫且 chatter 直接張貼關 → 拒絕並指向 `chatter_post`（在 create／write／unlink 之前，所以 `mail.message`
   的 create 在直接寫入開時也擋）；
4. create／write／unlink → 直接寫入開才執行，否則拒絕並指向三階段審批；
5. 上游依名字判成唯讀的 `get_*`，而且直接寫入與未審查方法都開 → 執行（與上游相同，不讀白名單）；
6. 白名單裡的 `model.method` → 執行；
7. 直接寫入關 → 拒絕；
8. 未審查方法關 → 拒絕（上游原文）；
9. 執行。

白名單只在第 6 步才讀（上游每次呼叫都重讀政策檔，讀取與 create／write／unlink 不讀，與上游相同）。拒絕上游判成唯讀的
`get_*` 時，回應的 `classification` 另外帶 `reviewed_read_only: false`。四個都開時每一步都放行，行為與第二輪（8c825a5）
逐位元相同。

## Permissions 頁的操作限制擋不住 execute_method

Permissions 頁只能限制三支審批工具（`preview_write`、`validate_write`、`execute_approved_write`）的 create／write／unlink
（`odoo_mcp_admin/tool_registry.py` 的 `OPERATION_POLICIES`）；`execute_method` 接受任意 model 方法，沒有有限的 selector，
要禁止只能整支停用或關掉開關。全開之後直接寫入是預設，所以管理員在 Permissions 頁關掉 unlink，`execute_method` 的 unlink
照跑。這一點以前只寫在程式註解裡；現在三支審批工具旁邊會顯示說明（registry 的 `OPERATION_NOTES` → `/api/tools` 的
`operation_note` → 共用的 `PermissionEditor`）：要禁止直接 create／write／unlink，請到 Settings 關 Allow Direct Writes，或在
Tools 頁停用 `execute_method`。

## 說明跟著開關（模型只能看說明判斷能不能寫）

`execute_method` 的 `method` 參數、`chatter_post`、`preview_write`、`validate_write`、`execute_approved_write` 的說明在
每種組合都如實描述「什麼會直接跑、什麼被拒、要改走哪個流程」，全開時與第二輪（正式站連接器看到的那份）逐字相同：

- `method` 參數有 7 種文字：全開、未審查方法關、chatter 關（未審查方法開／關各一）、直接寫入關（chatter 開／關各一）、
  總閘門關。行為一樣的組合才共用同一句（直接寫入關時未審查方法開關沒有作用；總閘門關時其他三個沒有作用）。
- 收緊之後（不是任何方法都直接跑）的每一句都列出會直接跑的讀取方法（整份清單）、寫明清單外的 `get_*` 不算讀取，並指出
  清單外的讀取改用 `search_records`、`read_record`、`aggregate_records`——模型只看得到說明與錯誤訊息（審查第三輪 G2）。
  直接寫入關時的拒絕訊息同樣列出清單與替代做法，不斷言被拒的方法會改資料（它可能只是不在清單上）。
  因此「直接寫入開、未審查方法關」那一句不再是第二輪的原文（第二輪沒有說哪些讀取會跑）；全開的文字不變。
- 三支審批工具：直接寫入開時是上游原文（審批可選用）；直接寫入關時加一句「這是 create／write／unlink 唯一的路」；
  總閘門關時加一句「核准無法執行」。
- `chatter_post`：直接張貼／先預覽再 approval＋`confirm=true`／總閘門關時拒絕，三種。

說明在子行程註冊工具時算一次（改 Settings 會重啟子行程）。

## health_check 與 validate_write

- `health_check` 的 `runtime.notes` 第 3 句原本是靜態的「execute_method 預設擋破壞性方法與未審查的副作用方法」，全開時是錯的；
  現在由 `write_gates.runtime_note` 依四個開關產生。`runtime` 裡的 `write_execution_enabled`、`direct_writes_enabled`、
  `unknown_execute_method_enabled`、`chatter_direct_enabled` 仍是開關本身的值；總閘門關時其他開關不生效，看第 3 句。
- `runtime.broad_unknown_method_mode` 的 `enabled`／`risk` 是**實際生效**的模式（`write_gates.unknown_methods_run`）：
  總閘門、直接寫入、未審查方法三個都開才是 `true`／`"broad"`，否則 `false`／`"off"`。上游只看
  `ODOO_MCP_ALLOW_UNKNOWN_METHODS`，總閘門或直接寫入關時仍回 `enabled: true`、`risk: "broad"`，讀結構化欄位的稽核者或
  模型會得到相反的結論（審查第三輪 G3）；開關本身的值看 `unknown_execute_method_enabled`。
- `validate_write` 一律回明確的 `valid` 布林：與 `success` 同一個判定（沒有 error 等級的 issue）；提早失敗（`*_from_path`
  用法不對、拿不到或拿到空的 fields_get、例外）都是 `valid: false`。既有欄位不變；能不能真的執行看
  `approval_status.stored`（要用 live metadata 驗）與總閘門。

## 收緊時與第二輪不同的地方（發版說明）

四個開關都開（全開，目前正式站與新安裝的預設）時行為、tools/list、說明與第二輪（8c825a5）逐位元相同。只有管理員**關掉**
其中一個以上時，第三輪起會不一樣——發版說明要寫，關掉開關之前也要先看一次該服務的白名單：

| 收緊的開關 | 第二輪 | 第三輪起 |
|---|---|---|
| 總閘門關 | 只擋 `execute_approved_write`：create／write／unlink、未審查方法、chatter 照其他三個開關直接寫 | 任何工具都不寫：`execute_method` 只跑審過的讀取（白名單也不跑），`chatter_post` 第一次呼叫就拒絕 |
| 直接寫入關 | 未審查方法開時 `action_archive` 這類照樣直接寫 | 只有審過的讀取與白名單直接跑（未審查方法開關沒有作用） |
| 直接寫入、總閘門或未審查方法關 | 上游依名字當唯讀的 `get_*`（含會寫入的 `get_portal_url`）在每種組合都照跑；不在上游唯讀名單的讀取（`read_group`、`web_search_read`…）在未審查方法關時被拒 | 只有審過的讀取清單直接跑：清單外的 `get_*` 與未審查方法同一套規則；`read_group`、`web_*` 讀取在每種組合都照跑 |
| chatter 直接張貼關 | 只改 `chatter_post`：`execute_method` 照直接寫入／未審查方法／白名單的規則照樣跑 `message_post`、`mail.message` 的 create、`mail.compose.message` 送信、完成活動 | 這些 chatter 張貼呼叫一律拒絕、指向 `chatter_post`，**白名單裡的也一樣**（例如列入白名單的 `res.partner.message_notify`、`mail.activity.action_feedback` 在 chatter 關時不再直接跑） |
| 任何一個關 | `health_check` 的 `broad_unknown_method_mode` 只看未審查方法開關 | 回實際生效的模式；收緊時的說明與拒絕訊息列出讀取清單與替代工具 |

PaaS 可透過 Settings → Reviewed Odoo methods 設定白名單，也保留未儲存時的容器環境來源與政策檔來源。
關掉 chatter 直接張貼時，即使白名單裡有 chatter 張貼呼叫（`write_gates.is_chatter_call` 判斷），也必須改走審批流程。

## 程式在哪

- `odoo_mcp_admin/write_gates.py`：純函式（不讀環境變數、不 import `odoo_mcp`），決定、說明、health_check 那一句都在這裡。
- `patches/fix_direct_writes.py`（建置期修補，PINNED／PATCHED digest 釘死）：只把上游接過來（`execute_method` 的 guard
  把 model 與 method 都傳給 `execute_method_refusal`，因為算不算 chatter 張貼要看兩者）——`write_policy.write_switches()`
  以強制那個開關的判斷式讀四個開關（總閘門＝上游 `writes_enabled`、直接寫入＝修補加的 `direct_writes_enabled`、未審查方法＝上游
  `truthy_env`、chatter＝上游 `chatter_direct_enabled`），`execute_method` 的兩段上游 guard 換成一次 `execute_method_refusal`、
  `chatter_post` 先問 `chatter_post_refusal`、五支工具的 `description` 與 notes 第 3 句改呼叫 `write_gates`、`validate_write`
  補 `valid`。修補後的 `odoo_mcp` 會 import `odoo_mcp_admin.write_gates`：找不到就起不來（fail closed）；Dockerfile 建置時
  實際載入一次修補後的 server（41 支工具）。只改 `write_gates.py` 不會動到 digest。
- 新增或改開關：同時改 `seed_defaults.FRESH_ENV`／`LEGACY_DEFAULTS`／`POLICY_KEYS`（LaunchPolicy 的 `env_keys`、
  `editable_env_keys`、`fresh_env`）、`write_policy.write_switches()`（修補）、`write_gates`，以及 Settings 頁的說明
  （`frontend/src/appConfig.jsx`）。

## 測試

- `apps/odoo/tests/test_write_gates.py`：16 種組合 × (model, method)（分類用上游真的 `classify_method_safety`）× 白名單的決定表，
  含 `get_portal_url`、`read_group`、`default_get` 與 chatter 張貼呼叫；讀取清單與 `is_chatter_call` 的正反例釘住；拒絕訊息指名是
  哪個開關、說明列出讀取清單與替代工具、chatter 關的說明寫出界線、`unknown_methods_run`；未改動的文字與上游 1.3.0／第二輪逐字相同。
- `packages/mcp-admin-core/tests/test_odoo_write_gates.py`：16 種組合各起一次真的 launcher（修補過的 `odoo_mcp`、假 Odoo），
  以假 Odoo 實際收到的 `execute_kw` 判斷有沒有寫入，並檢查每種組合的說明、health_check 第 3 句與 `valid`；全開時 tools/list
  的 sha256 與第二輪 8c825a5 相同、送到 Odoo 的呼叫與回傳也相同。8c825a5 上跑同一個檔：16 種組合全部失敗（總閘門關時
  create 照樣送到 Odoo、直接寫入關時 action_archive 照樣送到 Odoo、chatter 關時 message_post 照樣送到 Odoo、`valid`
  不存在），全開的兩項守線通過。第三輪審查後另外跑 `get_portal_url`（account.move、sale.order）、`read_group`、
  `web_search_read`、`default_get`、`mail.message.create`、`mail.compose.message.action_send_mail`、
  `mail.activity.action_feedback`、`res.partner.activity_feedback`，並檢查 `broad_unknown_method_mode`。
