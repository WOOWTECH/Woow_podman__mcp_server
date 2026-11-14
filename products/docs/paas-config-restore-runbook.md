# PaaS MCP 服務設定卷還原作業手冊

這份手冊說明：要把 PaaS 上某一個 MCP 服務（mcp-server chart：Odoo、n8n、Hermes、LiteLLM、Headscale…）的設定卷還原成舊備份時，怎麼做才**不會讓已撤銷的 token 或舊管理密碼重新生效**。

平台目前沒有 MCP 設定卷的還原功能，以下全部是人工程序，每一次都要人工核准。程式行為與設計理由見 [restore guard 文件](security/mcp-config-restore-candidate-20261112.md)。

## 為什麼不能直接回滾

設定卷存有每個 profile 的 MCP token、撤銷紀錄、管理密碼與工具政策。直接把卷回滾到舊快照，或把舊檔案放回去，**備份之後才撤銷的 token、改掉的密碼都會重新有效**。這個問題已在隔離環境以真的 Odoo 重現。

所以還原一律要用映像內的工具 `python -m mcp_admin_core.restore_inplace`。它會做三件事：
1. 把備份內容交給 restore guard。guard 撤銷所有 MCP token，換掉管理密碼與管理 session generation，n8n 的子行程 bearer 也一併更新。
2. 以「還原進行中標記」保證：還原中斷時服務拒絕啟動，不會以半套或舊的資料對外服務。
3. 最後才發布新的設定。

工具只重設 MCP 憑證。帳號清單、名稱、後端連線（後端帳號與密碼）、工具政策、寫入開關與 Odoo 的 Reviewed Odoo methods 都會回到備份當時，所以手冊在步驟 1 先記下現況，步驟 9 在發 token 之前核對政策與每個帳號的後端帳號（多帳號實測 X1）。

## 前提（缺一不可）

- **服務映像必須是含本工具的新版**（本次安全發版之後的映像：含 restore guard、`restore_inplace`、還原標記檢查與 restore-policy 執行期修補）。
  - 正式站目前的 8297aa4／0e432c3 映像沒有這些功能，不可用來執行還原，也不可在還原後用舊映像啟動：舊映像看不懂標記，可能把工具政策改回全開。
  - 先依一般流程把服務升級到新映像，再做還原。
  - 映像還必須包含「略過 `.config.json.reset-backup-<32 位小寫 hex>`」的 guard 修正（審查編號 restore-1）。沒有這個修正的映像，會因為備份裡有這個檔案而拒絕整個還原（exit 1，步驟 `prepare`）。步驟 1 的前置檢查（restore-4）只確認映像有 `restore_inplace` 等功能，不檢查這項修正；用沒有這項修正的映像時，要先確認來源備份卷沒有 `.config.json.reset-backup-*`。
  - **Headscale**：映像還必須包含「略過設定卷根目錄的 `admin-sessions.sqlite3`」的 guard 修正（2026-11-13 多帳號實測 P1，見「已知問題」）。Headscale 管理程式啟動時沒有這個檔就會建立，所以 Headscale 卷的備份都有它；沒有這項修正的映像（例如 e0a80d6）會在步驟 1 第二段的備份檢查回 exit 1、`restore_source_rejected`，步驟 5 也會在 `prepare` 以 exit 1 停下並留下標記（處理方式與上一項 reset-backup 相同）。步驟 1 的映像檢查同樣看不出映像有沒有這項修正。
- 需要 tenant namespace（`paas-ws-*`）的寫入權限。
- 「把 Longhorn 備份還原成一顆新卷」需要 `longhorn-system` 的權限，依平台 repo 規則屬於叢集運維。請叢集運維執行，或由授權人員在 Longhorn UI 操作；本手冊其餘步驟只動該服務的 namespace。
- 事先通知使用者：還原期間服務中斷，還原後所有 MCP 網址（token）都要重新發放。
- 事先和服務負責人約好：步驟 1 要給他看帳號清單，請他說明備份之後做過的刪除、新增、改名、政策變更，以及改過哪些帳號的後端連線（換了後端使用者、資料庫或網址），並決定保留哪些（步驟 9）。維護期間不要在管理頁改帳號、政策或連線。
- 操作者電腦要有 `bash` 與 `python3`（步驟 4、9 在本機執行 python3）。

## 步驟

以下變數的意思：
- `NS`：服務的 namespace。
- `REL`：Helm release，也就是 label `app.kubernetes.io/instance` 的值，例如 `svc-1e199474`。

平台開的服務，Deployment 通常是 `${REL}-mcp-server`，設定卷 PVC 是 `${REL}-mcp-server-data`。但有例外，例如 Headscale 的 `hs324-mcp-admin` 與 `hs324-mcp-admin-data`。**一律用 label 查出實際名稱**，不要用猜的。

### 1. 記錄現況並固定映像（審查編號 restore-4、r2co-5、D3；帳號清單：多帳號實測 X1）

**必須在步驟 4 scale down 之前做**：scale 到 0 之後就沒有執行中的 Pod，無法再從 `.status.containerStatuses[].imageID` 取得實際在跑的映像，也讀不到帳號清單。

```bash
K="kubectl --context woow-k3s -n $NS"
DEPLOY=$($K get deploy -l app.kubernetes.io/instance=$REL -o jsonpath='{.items[0].metadata.name}')
PVC=$($K get deploy $DEPLOY -o jsonpath='{.spec.template.spec.volumes[?(@.name=="data")].persistentVolumeClaim.claimName}')
echo "$DEPLOY $PVC"
$K get pvc $PVC -o jsonpath='{.spec.volumeName} {.spec.storageClassName}{"\n"}'
# 執行中 Pod 實際使用的映像 digest（tag 可能被重推，spec 裡的 tag 不可信）
CONTAINER=$($K get deploy $DEPLOY -o jsonpath='{.spec.template.spec.containers[0].name}')
IMAGE_IDS=$($K get pods -l app.kubernetes.io/instance=$REL --field-selector=status.phase=Running \
  -o jsonpath="{range .items[*]}{.status.containerStatuses[?(@.name=='$CONTAINER')].imageID}{'\n'}{end}" | sed 's#^docker-pullable://##' | grep . | sort -u)   # grep .：去掉空行（同 label 下有 Pod 沒有同名 container 時 jsonpath 會輸出空行）（F9 驗證 P3）
echo "$IMAGE_IDS"
# r2co-5、D3：把 IMAGE_IDS 存成 IMAGE。剛好一行且格式為 <repo>@sha256:<64 位小寫 hex> 才設定；否則停下，不要往下做
if [ "$(printf '%s\n' "$IMAGE_IDS" | grep -c .)" = 1 ] && printf '%s' "$IMAGE_IDS" | grep -Eq '^[^@[:space:]]+@sha256:[0-9a-f]{64}$'; then
  IMAGE="$IMAGE_IDS"; echo "IMAGE=$IMAGE"
else
  unset IMAGE; echo "STOP: IMAGE_IDS 不是剛好一行 repo@sha256:<64 hex>，見下方處理方式" >&2
fi
```

- **`$K` 是 shell 變數**（不是 alias 或 function），所以可以直接接在 `timeout` 後面：`timeout 60 $K logs …` 會執行 `timeout 60 kubectl --context woow-k3s -n <NS> logs …`。這份手冊的指令以 **bash** 為準：zsh 預設不會把未加引號的 `$K` 拆成多個字，要改用 bash，或先 `setopt SH_WORD_SPLIT`（D4）。手冊依序執行的區塊中，每個 kubectl 指令都有時間上限（`--timeout`、`--request-timeout`、`timeout`）或是有次數上限的迴圈，不會無限等待（D4）。
- `IMAGE_IDS` 必須剛好一行，且格式為 `<repo>@sha256:<64 位 hex>`（例如 `registry.example/odoo-mcp-admin@sha256:0123…`）。上面的指令在符合時才設定 `IMAGE`，其餘情況會印出 `STOP` 並**取消設定** `IMAGE`（`unset`），後面的步驟因此不會拿到過期或空的值。**看到 `STOP` 就不要繼續**，依情況處理（r2co-5、D3）：
  - 零行：沒有 Running 的 Pod（服務已被關掉、Pod 在 CrashLoop 或 Pending）。這時無法從 Pod 取得實際映像，請叢集運維確認實際在跑的映像 digest（例如服務最近一次部署的紀錄）；**不要**改用 Deployment spec 裡的 tag，也不要自己猜。
  - 多行：Pod 之間的映像不一致（例如滾動更新進行中、或兩種映像並存）。等 rollout 結束後重跑本步驟；仍然多行就請叢集運維確認哪一個才是服務要還原的映像。
  - 只有 `sha256:…` 沒有 repo：節點的 runtime 沒回報 repo，請叢集運維提供完整的 `repo@sha256:…`。
- 空行會被 `grep .` 先濾掉再計數，所以「空行加一行合法 digest」會得到乾淨的單行 `IMAGE`，不會帶前導換行（F9 驗證 P3）。
- `IMAGE` 一定要在同一個 shell session 設定並使用（變數不會跨 session）；如果換了終端機，重跑這個指令區塊。Job 的 `image:` 一律用它（`repo@sha256`），不要用 tag。這樣 Job 與服務是同一份程式碼，也不會因為 tag 被重推而用到別的版本。
- 記下 Deployment、PVC、PV 名稱、StorageClass 與 `IMAGE`。下面的指令與 Job 範本用 `$DEPLOY`、`$PVC`、`$IMAGE` 表示。

**記錄帳號清單、政策與後端帳號（多帳號實測 X1，2026-11-13）**

還原會把整個設定卷帶回備份的時間點：帳號（profile）清單、每個帳號的名稱、後端連線（後端帳號與密碼）、工具政策（Tools、Permissions 頁）、寫入開關與 Odoo 的 Reviewed Odoo methods（都在 Settings 頁）都會變回備份當時。只有 MCP 憑證會重設（token、管理密碼、管理 session generation、n8n 子程序 bearer）。所以：
- 備份之後才刪掉的帳號會回來，連同它的後端帳密；
- 備份之後才新增的帳號會消失；
- 備份之後才加的限制會消失，例如停用的工具、停用的操作、關掉的寫入開關、從 Reviewed Odoo methods 拿掉的方法；
- 備份之後換過的後端帳號（例如從權限大的 Odoo 使用者換成受限使用者、換了資料庫或網址），會換回備份當時的帳號與密碼。

還原後管理頁看不出這些差異，每個帳號都只顯示「已撤銷」。如果直接對每個帳號發新 token，刪掉的帳號就重新可用，消失的限制也不會回來，新 token 的持有人還會用備份當時的後端帳號讀寫資料（舊 token 都不會復活，問題出在新發的 token）。只有一個帳號的服務也一樣：預設帳號在備份之後加的限制、換過的後端帳號同樣會變回去。

所以現在（服務還在跑）先記下現況，步驟 9 再拿還原後的狀態來比對。下面的 `profile-inventory.py` 只讀設定卷，輸出每個帳號的：
- id、名稱、token 狀態（`active`／`revoked`／`none`）；
- 停用的工具與操作、寫入開關；
- Reviewed Odoo methods（`mcp_server.odoo_allowed_methods`，只有 Odoo）：`null`＝沒有存過，子程序沿用容器環境的清單；`[]`＝存過空清單，不允許任何方法；
- 後端帳號（只有 Odoo：網址、資料庫、使用者，即 `LaunchPolicy.identity_connection_keys` 裡不是秘密的欄位）。其他產品沒有宣告後端帳號欄位（帳號多半由 API key、app password、kubeconfig 這類秘密決定），帳號清單不記，步驟 9 要在 Connection 頁逐一確認。

**它不輸出任何憑證**：MCP token、管理密碼、後端密碼與 API key、n8n 子程序 bearer 都不讀出，token 只記狀態；網址裡的帳密（`user:pass@`）、query 與 fragment 也遮成 `***`。

把下面的內容存成 `profile-inventory.py`，和 `preflight.py`、`restore-job.yaml` 放在同一個工作目錄：

```python
# profile-inventory.py：記下帳號清單、政策與後端帳號，還原後比對（手冊步驟 1、9；2026-11-13 多帳號實測 X1）。
# 只讀設定卷，不輸出任何憑證：MCP token、管理密碼、後端密碼與 API key、n8n 子程序 bearer 都不讀出（token 只記狀態）。
# 後端連線只記 IDENTITY 列的帳號欄位；網址裡的帳密（user:pass@）、query 與 fragment 也不記。
#   在服務 Pod 內：python3 - inventory < profile-inventory.py    JSON 印到 stdout，每個帳號一行摘要印到 stderr
#   在操作者電腦：python3 profile-inventory.py diff <步驟 1 的檔> <現在的檔> [<還原後的檔>]    沒有差異才印 RESULT: OK、exit 0
import json, os, re, sys

# 各產品 Settings 頁的寫入開關（各 app LaunchPolicy.editable_env_keys 的聯集，tests/test_restore_runbook.py 比對）。
# mcp_server.env 的其他鍵（例如 n8n 的 AUTH_TOKEN）一律不讀出。
SWITCHES = ("ODOO_MCP_ENABLE_WRITES", "ODOO_MCP_ALLOW_DIRECT_WRITES", "ODOO_MCP_ALLOW_UNKNOWN_METHODS",
            "MCP_CHATTER_DIRECT", "LITELLM_MCP_READONLY", "K8S_MCP_READ_ONLY", "K8S_MCP_DISABLE_DESTRUCTIVE",
            "K8S_MCP_ALLOW_SECRETS", "K8S_MCP_TOOLSETS", "K8S_MCP_MULTI_CLUSTER")
# 後端帳號：各 app LaunchPolicy.identity_connection_keys 裡不是秘密的鍵（目前只有 Odoo；tests 比對）。
IDENTITY = ("odoo_url", "odoo_db", "odoo_username")
# (欄位, 差異標記, 到哪裡改)。store.patch 寫進 mcp_server／tools 的每帳號政策都要在這裡（tests 比對）。
FIELDS = (("label", "名稱", "Profile 選單用 Rename "), ("tools_disabled", "工具", "Tools 頁"),
          ("tools_disabled_operations", "操作", "Permissions 頁"), ("switches", "開關", "Settings 頁"),
          ("odoo_allowed_methods", "方法", "Settings 頁的 Reviewed Odoo methods "),
          ("backend", "後端", "Connection 頁（密碼請服務負責人提供）"))


def url(value):   # 只記 scheme://host:port/path：網址裡的帳密（到最後一個 @ 為止）、query、fragment 可能是憑證
    m = re.match(r"([^:/?#]*://)?([^?#]*@)?([^?#]*)(.*)\Z", value, re.S)
    return (m.group(1) or "") + ("***@" if m.group(2) else "") + m.group(3) + ("?***" if m.group(4) else "")


def policy(cfg):
    tools, server, conn = cfg.get("tools") or {}, cfg.get("mcp_server") or {}, cfg.get("connection") or {}
    disabled = tools.get("disabled") or []
    disabled = disabled.split(",") if isinstance(disabled, str) else disabled
    ops = tools.get("disabled_operations") or {}
    # Reviewed Odoo methods：None＝沒有存過（子程序沿用容器環境的清單）；[]＝存過空清單（不允許任何方法）
    methods = server.get("odoo_allowed_methods")
    backend = {k: conn[k] for k in IDENTITY if k in conn}
    if (not isinstance(disabled, list) or not isinstance(ops, dict) or not all(isinstance(v, list) for v in ops.values())
            or not (methods is None or isinstance(methods, list) and all(isinstance(m, str) for m in methods))
            or not all(v is None or isinstance(v, str) for v in backend.values())):
        raise ValueError("policy")
    env = server.get("env") or {}
    return {"token": "revoked" if cfg.get("mcp_auth_revoked") else "active" if cfg.get("mcp_auth_token") else "none",
            "tools_disabled": sorted(set(str(t).strip() for t in disabled) - {""}),
            "tools_disabled_operations": {str(k): sorted(str(x) for x in v) for k, v in ops.items()},
            "switches": {k: env[k] for k in SWITCHES if k in env},
            "odoo_allowed_methods": None if methods is None else sorted(methods),
            "backend": {k: url(v) if k.endswith("_url") and v else v for k, v in backend.items()}}


def inventory():
    path = os.environ.get("MCP_ADMIN_CONFIG") or "/data/config.json"
    try:
        with open(path) as fh:
            root = json.load(fh)
        rows = [dict(policy(root), id="default", label="Default")]
        index = list((root.get("profile_index") or {}).items())
    except (OSError, ValueError, AttributeError, TypeError):
        print("STOP: 讀不到 %s，沒有輸出帳號清單" % path, file=sys.stderr)
        return 2
    for pid, item in index:
        row = {"id": pid, "label": item.get("label") if isinstance(item, dict) else None}
        try:
            if not re.match(r"[0-9a-f]{24}\Z", pid):
                raise ValueError("profile id")
            with open(os.path.join(os.path.dirname(path), "profiles", pid, "config.json")) as fh:
                row.update(policy(json.load(fh)))
        except (OSError, ValueError, AttributeError, TypeError):
            row["error"] = "unreadable"
        rows.append(row)
    print(json.dumps({"inventory": "mcp-profile-policy", "profiles": rows}, indent=1, sort_keys=True))
    for row in rows:   # %r：名稱與值裡的控制字元以跳脫形式顯示
        print("%s %r " % (row["id"], row["label"]) + ("讀不到這個帳號的設定" if "error" in row else
              "token=%(token)s 停用工具=%(tools_disabled)r 停用操作=%(tools_disabled_operations)r 開關=%(switches)r "
              "方法清單=%(odoo_allowed_methods)r 後端帳號=%(backend)r" % row), file=sys.stderr)
    return 1 if any("error" in row for row in rows) else 0


def load(path):
    with open(path) as fh:
        data = json.load(fh)
    if data.get("inventory") != "mcp-profile-policy":
        raise ValueError(path)
    return {p["id"]: p for p in data["profiles"]}


def diff(before_file, now_file, restored_file=None):
    try:
        before, now = load(before_file), load(now_file)
        restored = set(load(restored_file)) if restored_file else set(now)   # 還原帶回來的帳號 id
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("STOP: 讀不到帳號清單檔；先跑完產生它的區塊")
        return 2
    show = lambda p: "%r（id %s）" % (p.get("label"), p["id"])
    fold = lambda p: str(p.get("label")).casefold()
    new = {fold(p): i for i, p in now.items() if i not in before and i not in restored}   # 核對時新建的帳號
    found = []

    def compare(old, cur):
        if "error" in old or "error" in cur:
            found.append("[讀不到] %s：設定讀不到，無法核對；到 Logs 頁找原因" % show(cur))
            return
        for key, tag, where in FIELDS:
            a, b = old.get(key), cur.get(key)
            if key in ("switches", "backend"):   # 開關與後端帳號逐一列出不同的那幾個
                a, b = a or {}, b or {}
                pairs = [(k + " ", a.get(k), b.get(k)) for k in sorted(set(a) | set(b))]
            else:
                pairs = [("", a, b)]
            for name, x, y in pairs:
                if x != y:
                    found.append("[%s] %s：%s步驟 1 是 %r，現在是 %r——到 %s改回步驟 1 的值" % (tag, show(cur), name, x, y, where))

    print("步驟 1：%d 個帳號；現在：%d 個帳號，其中還原帶回來的 %d 個" % (len(before), len(now), len(restored & set(now))))
    for i, cur in now.items():
        if i in restored and "error" not in cur and cur.get("token") != "revoked":
            found.append("[token] %s：還原帶回來的帳號 token 是 %s，應該是 revoked——還原可能沒有真的執行，停止，回到步驟 4、5"
                         % (show(cur), cur.get("token")))
        if i in before:
            compare(before[i], cur)
        elif i in restored:
            found.append("[刪除] %s：步驟 1 沒有這個帳號，是還原帶回來的（備份之後被刪掉）——向服務負責人確認後在 Profile 選單刪除，刪除前不要對它發 token" % show(cur))
    for i, old in before.items():
        if i in now:
            continue
        j = new.pop(fold(old), None)
        if j is None:
            found.append("[重建] %s：步驟 1 有、現在沒有（備份之後才新增的帳號）——依服務負責人決定在 Profile 選單以同名重建，再照步驟 1 設定："
                         "停用工具 %r、停用操作 %r、開關 %r、Reviewed Odoo methods %r、後端帳號 %r（密碼請負責人提供）"
                         % (show(old), old.get("tools_disabled"), old.get("tools_disabled_operations"), old.get("switches"),
                            old.get("odoo_allowed_methods"), old.get("backend")))
        else:
            print("[已重建] %s已重建為 id %s" % (show(old), j))
            compare(old, now[j])
    for j in new.values():
        found.append("[多出] %s：步驟 1 沒有，也不是還原帶回來的——確認是不是多建了，不要的就刪除" % show(now[j]))
    for line in found:
        print(line)
    if found:
        print("RESULT: %d 項差異。處理完再跑一次步驟 9 的檢查區塊；要保留的差異必須由服務負責人決定並記在工單上" % len(found))
        return 1
    print("RESULT: OK（帳號清單、名稱、工具政策、寫入開關、Reviewed Odoo methods 與記下的後端帳號都與步驟 1 相同；"
          "還原帶回來的帳號 token 都是 revoked）")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["inventory"]:
        sys.exit(inventory())
    if sys.argv[1:2] == ["diff"] and len(sys.argv) in (4, 5):
        sys.exit(diff(*sys.argv[2:]))
    print("用法：python3 - inventory < profile-inventory.py ｜ python3 profile-inventory.py diff 步驟1.json 現在.json [還原後.json]", file=sys.stderr)
    sys.exit(64)
```

在步驟 1 同一個 shell、同一個工作目錄執行。用到本步驟上面設定的 `$DEPLOY`、`$CONTAINER`：

```bash
: "${DEPLOY:?先跑本步驟的第一個區塊}" "${CONTAINER:?先跑本步驟的第一個區塊}"
if [ -e profiles-before.json ]; then
  echo "STOP: profiles-before.json 已經存在。還原開始（步驟 5）之後不可重跑這個區塊；還原前要重記，先把舊檔改名" >&2
elif timeout 60 $K exec -i deploy/$DEPLOY -c "$CONTAINER" -- python3 - inventory < profile-inventory.py > profiles-before.json.tmp; then
  mv profiles-before.json.tmp profiles-before.json && echo "OK: 已存 profiles-before.json"
else
  echo "STOP: 沒有讀到完整的帳號清單（profiles-before.json.tmp），見下方說明" >&2
fi
```

- 畫面上每個帳號一行摘要（名稱與值裡的控制字元以跳脫形式顯示），完整紀錄存在 `profiles-before.json`。這個檔不含憑證，可以附在工單上；Odoo 服務的紀錄含後端網址、資料庫與使用者名稱（不含密碼）。
- 把這份清單給服務負責人看，確認備份之後（選定的備份時間點之後）有沒有刪除、新增、改名或改政策，以及哪些要保留。
- 也請負責人逐一說明：備份之後改過哪些帳號的後端連線（換了後端使用者、資料庫或網址），現在每個帳號應該連哪個後端帳號。記在工單上，步驟 9 發 token 之前照這份說明逐一確認。Odoo 的後端帳號也在這份紀錄裡，可以直接對照。
- 步驟 9 以這份紀錄與負責人的說明為準；負責人決定不照紀錄的項目，要記在工單上。
- `STOP: 沒有讀到完整的帳號清單`：服務沒有在跑，或某個帳號的設定讀不到（`profiles-before.json.tmp` 裡該帳號有 `"error"`）。請服務負責人提供帳號清單、各帳號的政策與後端帳號，以手動方式記錄；步驟 9 改成人工逐項核對。
- `profiles-before.json` 已經存在時，這個區塊不會覆寫。還原開始之後再記，記到的就是還原後的狀態，步驟 9 會看不出任何差異。
- 從步驟 1 到步驟 4 之間，如果又有人改了帳號、政策或連線，在步驟 4 縮到 0 之前把舊檔改名，重跑這個區塊。

**前置檢查：確認映像含 restore_inplace（restore-4、r2co-5、r2-sec-4、D3）**

這個檢查是隔離演練（D2、D3）用的 preflight 改寫而成（D2 版在 `artifacts/mcp-security-release-20261112/d2-restore-drill-final/mcp.py` 的 `PREFLIGHT`）。D3 發現舊版「映像檢查」與「備份來源檢查」共用同一支程式：映像支援時，沒掛備份卷的檢查 Job 只會留下讀 `/restore-source` 的 `FileNotFoundError` traceback（exit 1），看不出映像檢查是否通過。現在拆成兩個模式（D3）：

- `image`：只檢查映像有沒有 restore 功能（`restore_inplace`、還原標記檢查、restore-policy 執行期修補、guard 的 `allow_volume_root`），不讀任何卷。
- `source`：先做同樣的映像檢查，再對 `/restore-source` 在 `/tmp` 實際跑 guard。

兩個模式的結果可明確區分（以 exit code 與輸出的 JSON 一行判斷，不看 traceback）。**exit 2 本身不代表映像不支援**：只有「exit 2 **且**輸出有 `"preflight": "refused"` 與 `image_lacks_restore_support` 的 JSON 行」才是映像不支援；沒有以 `{"preflight"` 開頭的 JSON 行的 exit 2（例如 python 找不到腳本，也是 exit 2）屬於其他錯誤（D4）：

| 結果 | exit | 輸出 | 動作 |
|---|---|---|---|
| 映像支援（`image` 模式） | 0 | `{"preflight": "image_ok", "product": …, "image_support": {四項皆 true}}` | 繼續 |
| 備份來源可還原（`source` 模式） | 0 | `{"preflight": "ok", …, "unexpected_entries": […]}` | 繼續 |
| 映像不支援 | 2 | `{"preflight": "refused", "reason": "image_lacks_restore_support", "image_support": {…}}`（必須有這一行 JSON，D4） | **停止**，先把服務升級到含 restore 功能的新映像 |
| 備份來源被 guard 拒絕 | 1 | `{"preflight": "refused", "reason": "restore_source_rejected", …}` | 停止，檢查備份內容（見下方 `unexpected_entries`） |
| 備份來源讀不到 | 1 | `{"preflight": "refused", "reason": "restore_source_unavailable", …}` | 停止，確認 `${REL}-restore-source` 有掛載 |
| 參數錯誤 | 64 | `{"preflight": "usage_error", …}` | 修正 command |
| 其他錯誤（拉不到映像、沒有 python、找不到腳本、Pod 被殺或被清掉、非預期例外） | 非 0（**包含 2**，例如 python 找不到腳本也是 exit 2），或沒有 Pod、Pod 沒有 exit code（D4） | 沒有以 `{"preflight"` 開頭的 JSON 行，或只有 traceback，或 `job_report` 印出 `NO POD` | 停止，不要把它當成「映像支援」；依 `job_report` 的提示與步驟 6 的方式看 Job 的 `Failed` 條件與 Pod 事件 |

輸出可能多一行 `JWT_SECRET not set in environment; generated a random secret…` 的警告（載入認證模組時印出，無害；本機驗證時 `image` 與 `source` 模式都有），以 JSON 那一行判斷，不要看第一行。只有 exit 0 **且**輸出有 `"preflight": "image_ok"`（或 `"ok"`）那一行，才算通過；不要只看「沒有 exit 2」。（D3 在舊映像 8297aa4 上得到 exit 2、`image_lacks_restore_support`，與本表一致；映像支援的情況 D3 當時跑的是舊版共用程式，只看到 traceback，這個新版的 `image_ok` 輸出，D4 已在叢集上驗證（映像支援、映像不支援、錯誤腳本路徑、拉不到映像）；F11 又以修正後的指令重驗一次，見「演練紀錄」。）

把下面的內容存成 `preflight.py`：

```python
import inspect, json, os, shutil, sys, tempfile
from pathlib import Path
mode = sys.argv[1] if len(sys.argv) > 1 else ""
product = sys.argv[2] if len(sys.argv) > 2 else "generic"
if mode not in ("image", "source") or product not in ("generic", "n8n"):
    print(json.dumps({"preflight": "usage_error", "usage": "preflight.py image|source [generic|n8n]"}))
    sys.exit(64)
try:  # this image must carry the guard, the restore marker check and the restore-policy runtime patch
    from mcp_admin_core import owner_defaults, restore_guard, restore_inplace, restore_marker
    support = {"restore_inplace": callable(getattr(restore_inplace, "restore", None)),
               "restore_marker_guard": hasattr(restore_marker, "RestoreInProgressGuard"),
               "restore_policy_hold": hasattr(owner_defaults, "RESTORE_POLICY_HOLD"),
               "guard_allow_volume_root": "allow_volume_root" in inspect.signature(restore_guard.prepare).parameters}
except ImportError:
    support = {}
if not support or not all(support.values()):
    print(json.dumps({"preflight": "refused", "reason": "image_lacks_restore_support", "image_support": support}))
    sys.exit(2)
if mode == "image":
    print(json.dumps({"preflight": "image_ok", "product": product, "image_support": support}))
    sys.exit(0)
src = Path("/restore-source")
try:
    top = {"config.json", ".config.json.operations.json", "restore-operation-journals"} | ({"n8n-mcp"} if product == "n8n" else set())
    unexpected = sorted(n for n in os.listdir(src) if n not in top | {"profiles", "lost+found"})
    if (src / "profiles").is_dir():
        for pid in sorted(os.listdir(src / "profiles")):
            if (src / "profiles" / pid).is_dir():
                unexpected += sorted("profiles/%s/%s" % (pid, n) for n in os.listdir(src / "profiles" / pid) if n not in top)
    read_only = bool(os.statvfs(src).f_flag & os.ST_RDONLY)
except OSError:
    print(json.dumps({"preflight": "refused", "reason": "restore_source_unavailable", "image_support": support}))
    sys.exit(1)
work = Path(tempfile.mkdtemp(prefix="mcp-restore-preflight-"))
try:
    restore_guard.prepare(src, work / "out", product=product, allow_volume_root=True)
    ok = True
except Exception:
    ok = False
finally:
    shutil.rmtree(work)
print(json.dumps({"preflight": "ok" if ok else "refused", "product": product, "image_support": support,
                  **({} if ok else {"reason": "restore_source_rejected"}),
                  "source_read_only_mount": read_only, "unexpected_entries": unexpected}))
sys.exit(0 if ok else 1)
```

**r2-sec-4：這個檢查不會偵測映像是否含 reset-backup 略過修正（restore-1）。** 四項 `image_support` 只看功能是否存在；略過 `.config.json.reset-backup-<32 位 hex>` 是 guard 內部的行為，檢查不到。映像缺這個修正時：
- `image` 模式照樣回 `image_ok`（不能靠它判斷）。
- 若備份來源根目錄有 `.config.json.reset-backup-*`，`source` 模式會回 exit 1、`restore_source_rejected`（`unexpected_entries` 會列出該檔）；步驟 5 的 Job 也會在 `prepare` 步驟以 exit 1 拒絕。**拒絕發生時，還原標記（`.mcp-restore-in-progress`）與工作目錄（`.mcp-restore-inplace/`）已經寫入 live 卷**（程式順序是 marker、workdir、prepare），但舊資料還沒移開（`move_aside` 之前），原本的 `config.json` 沒有被動。所以是 fail closed，不會毀損資料，只是還原做不下去；工具輸出為「failed at step prepare; the restore marker is kept」，與步驟 6 的 exit 1 列一致（F9 驗證 P2、r2-sec-4）。處理：服務維持 scale 0（不要拉回 1，否則標記會讓它 CrashLoopBackOff、exit 3），換含修正的映像後，依步驟 6 的 exit 1 列重跑步驟 5 的區塊；若決定放棄還原，標記仍在，必須依步驟 6 末尾「要放棄還原」的條件處理（只有舊資料還沒移開時才可先刪工作目錄再刪標記），不能當作沒事。
- 辨識方式：確認 `$IMAGE` 的 digest 是本次安全發版（含 restore-1 修正）或之後建置的映像，向建置紀錄對照 digest，不要憑 tag 判斷。`source` 模式的拒絕本身**不足以**推論映像缺修正：大寫 hex、symlink、目錄、profile 目錄內的同名檔，在有修正的映像上也會被拒絕並列在 `unexpected_entries`（F9 驗證 P3，已實測）。只有在 `unexpected_entries` 的檔名**完全符合** `.config.json.reset-backup-` 加 32 位**小寫** hex，且該項是**一般檔案**（不是 symlink、不是目錄、位於來源根目錄而非 profile 子目錄）時，才能推論為映像缺修正。處理：換用含修正的映像，不要手動刪備份卷上的檔案。

**準備：ConfigMap、建立 Job、等待 Job 與讀結果的函式**

```bash
$K delete configmap mcp-restore-preflight --ignore-not-found --timeout=120s   # 重跑時舊的同名 ConfigMap 會讓 create 失敗（already exists），先刪（F9 驗證 P3）
$K create configmap mcp-restore-preflight --from-file=preflight.py
new_job() {   # 用法：new_job <job 名稱> <manifest 檔，- 表示 stdin>。先刪同名的舊 Job、等它和它的 Pod 都消失，再 create（不用 apply）（多帳號實測 P3-d）
  JOB_UID=    # 成功：印 created uid=…，JOB_UID 是這次的 Job uid，回傳 0。任何一步失敗：印 STOP，JOB_UID 留空，回傳 1
  $K delete job/$1 --ignore-not-found --cascade=foreground --timeout=120s </dev/null || { echo "STOP: 刪不掉舊的 job/$1（或逾時），不要往下" >&2; return 1; }
  if [ -n "$($K get job/$1 --ignore-not-found -o name --request-timeout=20s </dev/null)$($K get pods -l job-name=$1 -o name --request-timeout=20s </dev/null)" ]; then
    echo "STOP: 舊的 job/$1 或它的 Pod 還在，不要往下" >&2; return 1
  fi
  JOB_UID=$($K create -f "$2" -o jsonpath='{.metadata.uid}') && [ -n "$JOB_UID" ] || { echo "STOP: 建立 job/$1 失敗（原因見上面 kubectl 的訊息），不要往下" >&2; JOB_UID=; return 1; }
  echo "job/$1 created uid=$JOB_UID"
}
wait_job() {   # 用法：wait_job <job 名稱> <new_job 給的 uid> [輪詢次數，預設 72]；每 5 秒同時看成功與失敗（restore-5），預設最多約 6 分鐘
  local i U S F FC
  for i in $(seq 1 "${3:-72}"); do
    IFS='|' read -r U S F FC <<<"$($K get --request-timeout=20s job/$1 -o jsonpath='{.metadata.uid}|{.status.succeeded}|{.status.failed}|{.status.conditions[?(@.type=="Failed")].status}')"
    [ -n "$U" ] && [ "$U" != "$2" ] && { echo "STOP: job/$1 已不是這次建立的 Job（uid 不同），不要往下" >&2; return 4; }
    [ "${S:-0}" -ge 1 ] && { echo "job succeeded"; return 0; }
    { [ "${F:-0}" -ge 1 ] || [ "$FC" = "True" ]; } && { echo "job failed"; return 1; }
    sleep 5
  done
  echo "job wait timeout"; return 2
}
job_report() {   # 用法：job_report <job 名稱> <new_job 給的 uid>；印出 exit code 與 log。Job 沒有 Pod 時不會卡住（D4）
  local pods p rc
  pods=$($K get pods -l job-name=$1 -o name --request-timeout=30s) || { echo "STOP: 查詢 Pod 失敗（API 連線或權限），不要往下" >&2; return 1; }
  if [ -z "$pods" ]; then
    # 拉不到映像、Pod 被刪、Job Failed 後 Pod 被清掉：沒有 Pod，就沒有 exit code 也沒有 log。
    # 直接 `$K logs job/$1` 在這種情況會一直等 Pod 出現而卡住（D4 實測 305～650 秒以上）
    echo "NO POD: job/$1 沒有 Pod，沒有 exit code，也沒有 log。請看：$K describe job/$1 與 $K get events --sort-by=.lastTimestamp" >&2
    return 3
  fi
  # 只讀這次建立的 Job 的 Pod：同名舊 Job 留下的 Pod 會印出上一次的結果（多帳號實測 P3-d）
  [ "$($K get pods -l job-name=$1 --request-timeout=30s -o jsonpath='{range .items[*]}{.metadata.ownerReferences[?(@.kind=="Job")].uid}{"\n"}{end}' | sort -u)" = "$2" ] \
    || { echo "STOP: job-name=$1 的 Pod 不全是 uid $2 的 Job 建立的（可能是舊 Job 留下的），不要看它們的結果" >&2; return 4; }
  # 每個 Pod 一行 `pod/<名稱> exit=<數字>`；Pod 還沒結束或被外力殺掉時 exit= 後面是空白（D4）
  $K get pods -l job-name=$1 --request-timeout=30s -o jsonpath='{range .items[*]}{.metadata.name}{" exit="}{.status.containerStatuses[0].state.terminated.exitCode}{"\n"}{end}'
  for p in $pods; do
    timeout 60 $K logs $p; rc=$?   # 讀 log 一律有 60 秒上限（D4）
    [ $rc = 0 ] || echo "log 讀取失敗或逾時（exit $rc）：$p 可能還沒啟動；看 $K describe $p" >&2
  done
}
```

這三個函式是這份手冊建立 Job 的唯一方式（前置檢查兩個 Job 與步驟 5 的還原 Job），不要改用 `$K apply -f`（多帳號實測 P3-d，2026-11-13）：
- **為什麼不用 `apply`**：同名的舊 Job 還在時，`apply` 根本不會建立新的 Job。manifest 相同時印 `job.batch/<名稱> unchanged`（舊 Job 是用 `create` 建的時候印 `configured`），不同時只在 stderr 印 `field is immutable`。接著的輪詢讀到上一次的 `succeeded`，`job_report` 印出舊 Pod 的 `exit=0` 與上一次的 log，看起來就像成功。
- **`new_job` 怎麼避免**：先刪掉同名的舊 Job。`--cascade=foreground` 會等它的 Pod 也刪完，Job 才算刪掉。接著確認 Job 與 Pod 都不在，再用 `create` 建立。同名 Job 還在時 `create` 會失敗，不會「unchanged」。
- **只認這一次的 Job**：`wait_job` 與 `job_report` 只認 `new_job` 給的 uid。Job 被別人換掉，或有不屬於這個 uid 的 Pod（例如舊 Job 留下的），就印 `STOP`，不讀它們的結果。
- **任何一步印 `STOP`**：都不是成功，依各區塊下方的說明處理。`new_job` 會刪掉同名的舊 Job，包括還在執行的（被中斷的還原可以重跑，見步驟 6），所以同一個服務同時只能有一個人做還原。
- **換了 shell**：要重貼這個區塊（重跑安全）。

**第一段：映像檢查（現在，不需要任何備份卷）**。下面的 Job 與步驟 5 的還原 Job 使用同樣的 securityContext 與掛載方式（非 root 1000:1000、fsGroup 1000、不掛 SA token、seccomp RuntimeDefault、丟棄所有 capabilities），只是不掛任何 PVC。指令要在步驟 1 同一個 shell 執行（用到 `$NS`、`$IMAGE`）；n8n 把 command 最後的 `generic` 換成 `n8n`：

```bash
: "${IMAGE:?先完成步驟 1，IMAGE 必須已設定}"
if new_job mcp-restore-preflight-image - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: mcp-restore-preflight-image
  namespace: $NS
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 300
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: preflight
          image: $IMAGE
          command: [python3, /code/preflight.py, image, generic]   # n8n：把 generic 換成 n8n
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
          resources:
            requests: {cpu: 50m, memory: 64Mi}
            limits: {cpu: 500m, memory: 256Mi}
          volumeMounts:
            - {name: code, mountPath: /code, readOnly: true}
      volumes:
        - name: code
          configMap: {name: mcp-restore-preflight}
EOF
then
  wait_job mcp-restore-preflight-image "$JOB_UID"
  job_report mcp-restore-preflight-image "$JOB_UID"   # 只讀這次建立的 Pod；先查 Pod 再讀 log，有時間上限（D4）
fi
$K delete job/mcp-restore-preflight-image --ignore-not-found --timeout=120s
```

依上面的結果表判斷：`exit=0` 且 log 有 `"preflight": "image_ok"` 才繼續。`exit=2` 且 log 有 `"preflight": "refused"` 與 `image_lacks_restore_support` 的那一行才是映像不支援，必須停止；`exit=2` 但沒有以 `{"preflight"` 開頭的 JSON 行（例如 python 找不到腳本），或 `job_report` 印出 `NO POD`（拉不到映像等），都是其他錯誤，同樣停止，但原因不同：看 `$K describe job/mcp-restore-preflight-image` 與 events，不要當成映像不支援（D4）。輸出依序是 `job/mcp-restore-preflight-image created uid=<uid>`、`job succeeded`（或 `job failed`）、`job_report` 的 `<pod 名稱> exit=<數字>`，接著是 log；沒有 Pod 時只印 `NO POD` 那一行（stderr）。印出 `STOP` 時這次沒有完成檢查，依「準備」區塊下方的說明處理後重跑本區塊。

**第二段：備份檢查（步驟 3 之後）**。同樣的 Job 加上唯讀的 `${REL}-restore-source`（掛到 `/restore-source`，與步驟 5 相同），模式改成 `source`。`exit=0` 且 log 有 `"preflight": "ok"` 才繼續。這一步會在 `/tmp` 實際跑 guard，live 卷與備份卷都不會被改（備份卷唯讀；Job 也不掛 live PVC）：

```bash
: "${IMAGE:?先完成步驟 1，IMAGE 必須已設定}"
if new_job mcp-restore-preflight-source - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: mcp-restore-preflight-source
  namespace: $NS
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 300
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: preflight
          image: $IMAGE
          command: [python3, /code/preflight.py, source, generic]   # n8n：把 generic 換成 n8n
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
          resources:
            requests: {cpu: 50m, memory: 64Mi}
            limits: {cpu: "1", memory: 512Mi}
          volumeMounts:
            - {name: code, mountPath: /code, readOnly: true}
            - {name: source, mountPath: /restore-source, readOnly: true}
      volumes:
        - name: code
          configMap: {name: mcp-restore-preflight}
        - name: source
          persistentVolumeClaim: {claimName: ${REL}-restore-source, readOnly: true}
EOF
then
  wait_job mcp-restore-preflight-source "$JOB_UID"
  job_report mcp-restore-preflight-source "$JOB_UID"   # 只讀這次建立的 Pod；先查 Pod 再讀 log，有時間上限（D4）
fi
$K delete job/mcp-restore-preflight-source --ignore-not-found --timeout=120s
```

兩段都過之後，可刪除 ConfigMap：`$K delete configmap mcp-restore-preflight`（步驟 13 也會提醒）。映像檢查沒過，不要繼續；請先把服務升級到含 restore 功能的新映像。

### 2. 建立回退點

請叢集運維對 live 卷建立 Longhorn snapshot。用途只有一個：還原完全失敗、要放棄時回到還原前的狀態。

回到這個快照等於「沒有還原」：還原前有效的 token 照樣有效，不會讓更早撤銷的 token 復活。

### 3. 把備份還原成新的 PVC

請叢集運維把選定的 Longhorn 備份還原成**一顆新卷**，並在同一個 namespace 建立 PVC `${REL}-restore-source` 綁定它。**不要對 live 卷直接回滾。**

這顆 PVC 之後只會被唯讀掛載。

### 4. 停止服務並確認沒有流量（D3）

```bash
$K scale deploy/$DEPLOY --replicas=0
# D3：scale 之後 Pod 仍會停留在 Terminating 一陣子，必須等到沒有任何 Pod（含 Terminating）才往下
$K wait --for=delete pod -l app.kubernetes.io/instance=$REL --timeout=300s   # 已經沒有 Pod 時立刻回傳 exit 0（D3 後以唯讀方式在叢集上確認）
[ -z "$($K get pods -l app.kubernetes.io/instance=$REL -o name)" ] && echo "no pods" || echo "STOP: 仍有 Pod，不要繼續"
$K get endpointslices -o json | python3 -c "import json,sys; print([e.get('endpoints') for e in json.load(sys.stdin)['items'] if e['metadata'].get('labels',{}).get('kubernetes.io/service-name','').startswith('$REL')])"   # 應為空或 None；若為 CrashLoop 期間，可能有 1 個 not-ready endpoint（見「已知問題」D2 P2-1）
```

還原期間**不要**在平台後台對這個服務執行重新部署、重設密碼或升級：那些動作會用 helm 把 replicas 改回 1。

縮到 0 之前：步驟 1 記錄帳號清單之後，如果又有人改了帳號、政策或連線，先把 `profiles-before.json` 改名，重跑步驟 1 的帳號清單區塊（服務停了就讀不到）。

`kubectl wait` 逾時（300 秒）或最後一行印出 `STOP`，代表 Pod 還在（可能卡在 Terminating）：先找出原因（`$K describe pod`、節點狀態），請叢集運維處理，**不要**在還有舊 Pod 時執行步驟 5。若 `kubectl wait` 因 Pod 在等待期間被刪除而回報 NotFound，或在沒有符合的 Pod 時回報 `no matching resources found`（舊版 kubectl 的行為；新版 v1.37 實測是直接 exit 0），都以最後的 `no pods` 為準（F9 驗證 P3）。

萬一服務被拉起來，還原標記會讓它起不來（CrashLoopBackOff），但已經在跑的舊 Pod 擋不住，所以一定要先確認沒有 Pod。

設定卷上的檔案只能在服務停止時修改：管理程序執行期間會快取設定，執行中直接改檔案的效果不確定（G1 實測：同樣的修改有時生效、有時沒有）。本手冊的步驟都在 scale 到 0 之後才動卷；不要在服務執行中手動修改設定卷。

### 5. 執行還原 Job

把下面的內容存成 `restore-job.yaml`，填入 `NS`、`REL` 與步驟 1 固定下來的 `IMAGE`（`repo@sha256:…`）。n8n 服務在 command 加上 `--product n8n`。

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: mcp-restore-inplace
  namespace: <NS>
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: restore
          image: <步驟 1 的 $IMAGE：repo@sha256:<64 位 hex>，不可用 tag>
          command: [python3, -m, mcp_admin_core.restore_inplace, /restore-source, /data]   # n8n：加 --product n8n
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: "1", memory: 512Mi}
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: source, mountPath: /restore-source, readOnly: true}
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: <$PVC：服務的設定卷 PVC>}
        - name: source
          persistentVolumeClaim: {claimName: <REL>-restore-source, readOnly: true}
```

這份範本與隔離演練實際使用的 Pod 安全設定相同：非 root 的 1000:1000、fsGroup 1000、不掛 SA token、seccomp RuntimeDefault、丟棄所有 capabilities。要另外加 `readOnlyRootFilesystem` 等限制，必須先在隔離環境驗證過。

```bash
if new_job mcp-restore-inplace restore-job.yaml; then   # 先刪掉同名的舊 Job（上一次的還原，成功、失敗都一樣）並等它消失，再建立（多帳號實測 P3-d）
  wait_job mcp-restore-inplace "$JOB_UID" 372          # 審查編號 restore-5：同時輪詢成功與失敗；372 × 5 秒 ≈ 1860 秒，略大於 activeDeadlineSeconds
  job_report mcp-restore-inplace "$JOB_UID"            # 只讀這次建立的 Pod；先查 Pod 再讀 log，有時間上限（D4）。log 是固定文字，不含任何憑證（可能多一行無害訊息，見下）
fi
```

`new_job`、`wait_job`、`job_report` 定義在步驟 1 的「準備」區塊；換了 shell 要重貼那個區塊（重跑安全）（D4）。

**判讀**：依序看到下面四項，才算這次的還原成功：
- `job/mcp-restore-inplace created uid=<uid>`；
- `job succeeded`；
- `<pod 名稱> exit=0`；
- 完成訊息 `MCP in-place restore completed…`。

**不要**改用 `$K apply -f restore-job.yaml`，也不要只重跑區塊裡的某幾行（多帳號實測 P3-d，2026-11-13）。成功的 Job 會保留到步驟 13（見步驟 6），之前若要再還原一次（例如發現選錯備份），舊寫法會出事：`apply` 只印 `unchanged`，輪詢讀到上一次的 `succeeded`，`job_report` 印出舊 Pod 的 `exit=0` 與完成訊息，看起來成功但其實沒有執行。這時卷上仍是還原前的資料：上一次還原之後才發的 token 照樣有效，帳號與政策也沒有回到這次選的備份。原因見步驟 1「準備」區塊下方的說明。

**印出 `STOP:`**：都不是成功，服務維持 scale 0，處理後重跑整個區塊：
- `刪不掉舊的 job/…`、`舊的 job/… 或它的 Pod 還在`：舊 Job 或它的 Pod 卡住，例如有 finalizer 或節點故障。kubectl 可能先印 `job.batch "mcp-restore-inplace" deleted`，120 秒後才印 `timed out waiting for the condition`，以 `STOP` 為準。用 `$K describe job/mcp-restore-inplace` 與 `$K get pods -l job-name=mcp-restore-inplace` 查看，請叢集運維處理。
- `建立 job/… 失敗`：原因見上面 kubectl 的訊息，例如配額、權限或 manifest 錯誤。修正後重跑。
- `已不是這次建立的 Job`、`Pod 不全是 … 建立的`：有人同時在建同名的 Job。先確認同一個服務只有一個人在做還原，再重跑。

**不要**直接用 `$K logs job/mcp-restore-inplace`：Job 沒有 Pod 時（Pod 被刪、Job Failed 後 Pod 被清掉）它會一直等 Pod 出現而卡住，D4 實測 305 秒以上（P2）。

D3：Job 的輸出可能多一行 `JWT_SECRET not set in environment; generated a random secret`（`mcp_admin_core.auth.middleware` 載入時，環境沒有設定 `JWT_SECRET` 就會印這行警告，還原工具會間接載入它），這是預期的無害訊息，不是錯誤；除此之外的輸出只有固定文字（成功時是完成訊息，失敗時是步驟名稱）。其他任何看起來像憑證、路徑或例外內容的行都不應該出現，出現就停下來回報。

`wait_job` 印 `job wait timeout`（逾時，既沒有成功也沒有失敗）時，先當作失敗處理：看 exit code 與 log，不要恢復服務。`.status.failed` 與 `Failed` 條件任一成立就算失敗（`backoffLimit: 0`，Pod 失敗後 Job 立刻失敗；逾 `activeDeadlineSeconds` 則由 `Failed` 條件反映）。

### 6. 依 exit code 處理（D3）

| exit | 意義 | 下一步 |
|---|---|---|
| 0 | 完成。所有 MCP token 已撤銷，管理密碼已換成沒有人知道的隨機值，標記已移除。 | 繼續步驟 7 |
| 1 | 某一步失敗，log 只寫步驟名稱。**標記保留，服務維持關閉。** | 依步驟名稱修正（空間不足、備份內容損壞、權限）後，**重跑步驟 5 的整個區塊**：`new_job` 會先刪掉這個 Job、等它和它的 Pod 消失，再建立新的。不要只跑 `apply`：同名 Job 還在時 `apply` 不會重跑。工具每次都從備份重新開始，不信任上一次的部分結果。不要手動刪標記。 |
| 2 | 參數錯誤。 | 修正 `restore-job.yaml` 的 command 後，重跑步驟 5 的區塊。 |
| 3 | 拒絕執行，這次什麼都沒改。原因可能是路徑不安全、以 root 執行，或另一個還原 Job 持有標記。 | 檢查 Job 設定，或等另一個 Job 結束，再重跑步驟 5 的區塊。 |

**exit 1 但 log 不是步驟名稱（D4）**：映像沒有 `restore_inplace`（例如正式站的舊映像 8297aa4）時，`python3 -m` 以 exit 1 結束，log 是 `No module named mcp_admin_core.restore_inplace`，不是步驟名稱，也沒有寫入任何標記。這表示步驟 1 的前置檢查被略過或判讀錯了：維持服務 scale 0，換含 restore 功能的映像後重跑步驟 5 的區塊（叢集上以舊映像驗證過這個輸出）。

**Job 被外力殺掉時沒有 exit code（D3、D4）**：Pod 被刪除、節點故障或逾 `activeDeadlineSeconds` 時，沒有 exit code，`job_report`（步驟 5）有兩種輸出，不會卡住也不會印 jsonpath 錯誤：Pod 還在但 `terminated` 不存在時，印 `<pod 名稱> exit=`（`exit=` 後面空白）；Pod 已經不存在（被刪、Job Failed 後被清掉）時，不印 exit code 與 log，只印一行 `NO POD: …`（stderr）並回傳 3。這種情況要看 Job 的 `Failed` 條件（`$K get --request-timeout=20s job/mcp-restore-inplace -o jsonpath='{.status.conditions[?(@.type=="Failed")]}'`）與 Pod 事件。當作 exit 1 處理：標記可能已寫入，服務維持關閉，重跑步驟 5 的區塊（它會先刪掉這個 Job）（D3 演練：在 `move_aside` 之後殺掉 Job，重跑同名 Job 後 exit 0）。

**完成的 Job 會保留**：`ttlSecondsAfterFinished: 86400` 讓 Job 在完成（成功或失敗）24 小時後才自動刪除。成功後 Job 與 Pod 仍留在 namespace，直到步驟 13 手動刪除（或 TTL 到期）；步驟 6、7 之間不需要處理它。步驟 13 之前要再還原一次時，重跑步驟 5 的區塊即可，`new_job` 會先刪掉這個 Job；同時把步驟 9 的 `profiles-restored.json` 改名，讓步驟 9 重新記錄這次還原後的狀態。

要放棄還原、回到還原前的資料：
- 只有在 live 卷上的 `.mcp-restore-inplace/pre` 不存在或是空的時候（舊資料還沒被移開）才可以先刪工作目錄、最後刪標記。
- 否則請用步驟 2 的 snapshot 回退，或重跑步驟 5 的區塊直到成功。
- 還原中斷期間，chart 0.1.10 之前的 seed init container 可能在卷上寫入一份新的 `config.json`（D2 P2-1，見「已知問題」）。放棄還原、不經 snapshot 回退時，要先確認卷上的 `config.json` 是不是原來的那一份，不能假設「卷內容不變」。
- **fsGroup 與權限（D3）**：在使用 `fsGroup` 的叢集（例如 Longhorn），kubelet 在讀寫掛載時可能調整卷上既有檔案的權限（例如 0600 變 0660、目錄加 setgid）。比對卷內容時，以 sha256、大小、mtime、inode 為準，**不要用 mode 判斷是否被改過**（D3 實測：直接比對 21 筆項目中有 13 筆只差 mode；用無作用的控制 pod 正規化後，chart pod 前後完全相同）。chart 0.1.10 的「seed 不改權限」指的是 seed 本身不改，不是 kubelet 不改。

### 7. 恢復服務

```bash
$K scale deploy/$DEPLOY --replicas=1
$K rollout status deploy/$DEPLOY --timeout=300s
timeout 60 $K exec deploy/$DEPLOY -- python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=5).read().decode())"
```

`/healthz` 應為 200。此時 MCP 端點對任何舊 token 都回 403，管理頁舊密碼無法登入。

### 8. 重設管理密碼

在平台後台對這個服務執行「重設管理密碼」，也就是 operator 的 `reset-config`。它的做法是：
1. 以 helm 更新 chart Secret。
2. 在執行中的 Pod 直接改寫 `config.json` 的 `admin_password`。
3. 重啟 Deployment。

整個過程**不需要知道舊密碼**，所以還原後的隨機密碼不影響它。新密碼依平台既有的帶外方式交給服務管理員。

做完這一步先不要發 token，接著做步驟 9。

### 9. 核對帳號清單、政策與後端帳號（發任何 token 之前；多帳號實測 X1）

步驟 8 之後，所有 token 仍是撤銷狀態，還沒有人能用 MCP。這一步拿還原後的狀態和步驟 1 的紀錄、負責人的說明比對，處理完差異、確認完每個帳號的後端帳號，**之後**才在步驟 10 發 token。為什麼要做，見步驟 1「記錄帳號清單、政策與後端帳號」。只有一個帳號的服務也要做。

在步驟 1 的工作目錄執行（需要 `profile-inventory.py`、`profiles-before.json`），用到步驟 1 的 `$K`、`$DEPLOY`、`$CONTAINER`。換了 shell 就重跑步驟 1 的第一個區塊（只讀，重跑安全）；**不要**重跑步驟 1 的帳號清單區塊。

**第一段：記下還原後的狀態，列出差異**。`profiles-restored.json` 記的是「還原帶回來的帳號」，只記一次：已經存在時沿用，不會覆寫。

```bash
: "${DEPLOY:?先跑步驟 1 的第一個區塊}" "${CONTAINER:?先跑步驟 1 的第一個區塊}"
$K rollout status deploy/$DEPLOY --timeout=300s   # 等步驟 8 的重啟完成，下面才會讀到新的 Pod
if [ -e profiles-restored.json ]; then
  echo "沿用已存的 profiles-restored.json（還原後、核對前的狀態）"
elif timeout 60 $K exec -i deploy/$DEPLOY -c "$CONTAINER" -- python3 - inventory < profile-inventory.py > profiles-restored.json.tmp; then
  mv profiles-restored.json.tmp profiles-restored.json
else
  echo "STOP: 沒有讀到完整的帳號清單（profiles-restored.json.tmp），先到 Logs 頁找原因" >&2
fi
python3 profile-inventory.py diff profiles-before.json profiles-restored.json
```

每一行差異以標記開頭。用步驟 8 的新密碼登入管理頁，依標記處理。管理頁上方的 Profile 選單可以切換帳號，也有 Create、Rename、Delete。

| 標記 | 意思 | 處理 |
|---|---|---|
| `[刪除]` | 步驟 1 沒有，是還原帶回來的帳號（備份之後被刪掉），連同備份裡的後端帳密 | 向服務負責人確認後，在 Profile 選單選它、Delete。刪除前不要對它發 token。刪除會撤銷它的 token、刪掉它的設定與後端帳密，之後只有再做一次還原才救得回來。只有負責人明確要救回這個帳號（還原的目的之一）時才保留，並記在工單上 |
| `[重建]` | 步驟 1 有、還原後消失的帳號（備份之後才新增的） | 依服務負責人決定，在 Profile 選單 Create 同名的帳號。新帳號會拿到新的 id 與 port，建立時會自動產生 token（到步驟 10 才交出去），而且會先複製預設帳號當下的工具政策、寫入開關與 Reviewed Odoo methods，連線是空的。所以一定要照這一行列出的值設定停用工具、停用操作、開關與 Reviewed Odoo methods，並在 Connection 頁填這一行列出的後端帳號（Odoo）或負責人說明的後端帳號（其他產品）；密碼由負責人提供（手冊不記錄任何憑證）。重建後第二段會逐項比對這些值 |
| `[名稱]` | 名稱和步驟 1 不同（備份之後改過名） | Profile 選單的 Rename，改回步驟 1 的名稱 |
| `[工具]` | 停用的工具和步驟 1 不同 | 切到該帳號，在 Tools 頁照步驟 1 的值設定 |
| `[操作]` | 停用的操作和步驟 1 不同 | 切到該帳號，在 Permissions 頁照步驟 1 的值設定 |
| `[開關]` | 寫入開關和步驟 1 不同 | 切到該帳號，在 Settings 頁照步驟 1 的值設定 |
| `[方法]` | Reviewed Odoo methods（Odoo 的 Settings 頁）和步驟 1 不同。Allow Unreviewed Methods 或 Allow Direct Writes 關閉時，`execute_method` 能跑哪些有副作用的方法由這份清單決定，多出來的方法就是放寬。`None`＝沒有存過（沿用容器環境的清單），`[]`＝存過空清單（不允許任何方法） | 切到該帳號，在 Settings 頁的 Reviewed Odoo methods 照步驟 1 的值設定（每行一項，順序不拘）。步驟 1 是 `None` 而現在有清單時，管理頁沒辦法改回「沒有存過」（存空清單會變成不允許任何方法）：由服務負責人決定要存哪一份清單，記在工單上 |
| `[後端]` | 後端帳號（Odoo 的網址、資料庫、使用者）和步驟 1 不同：備份之後換過後端帳號，還原換回了備份當時的帳號與密碼；或核對時重建的帳號還沒填連線 | 向服務負責人確認後，切到該帳號，在 Connection 頁改回步驟 1 的值，密碼由負責人提供。**改好之前不要對它發 token**：新 token 的持有人會用備份當時的後端帳號讀寫資料。只有負責人明確決定沿用備份當時的後端帳號時才保留，並記在工單上 |
| `[token]` | 還原帶回來的帳號 token 不是 `revoked` | **停止**。還原可能沒有真的執行（例如讀到上一次的還原 Job，見步驟 5），回到步驟 4、5。這一項只在步驟 10 發 token 之前有意義：發了 token 之後再比對，每個還原帶回來的帳號都會出現這一行，屬於預期 |
| `[讀不到]` | 這個帳號的設定讀不到 | 停止，到 Logs 頁找原因 |
| `[多出]` | 步驟 1 沒有，也不是還原帶回來的（只在第二段出現） | 確認是不是核對時多建了，不要的就刪除 |

**第二段：處理完之後再比一次**。這一段每次都重新讀取現況，可以重跑：

```bash
timeout 60 $K exec -i deploy/$DEPLOY -c "$CONTAINER" -- python3 - inventory < profile-inventory.py > profiles-now.json \
  && python3 profile-inventory.py diff profiles-before.json profiles-now.json profiles-restored.json
```

- 最後一行要是 `RESULT: OK` 才繼續。比對時，核對中重建的帳號用名稱對回步驟 1 的帳號，會印 `[已重建] '<名稱>'（id <舊 id>）已重建為 id <新 id>`（這行只是說明，不算差異）。
- 還原帶回來的帳號不能當成重建的帳號，即使同名也不行，因為它帶著備份裡的後端連線，仍會列為 `[刪除]`。
- 還有差異時，只有服務負責人明確決定保留的項目可以留下（例如還原的目的就是救回被誤刪的帳號）。每一項都記在工單上，才可以繼續。
- 比對的項目：帳號、名稱、工具政策、寫入開關、Reviewed Odoo methods，以及 Odoo 的後端帳號（網址、資料庫、使用者）。**後端密碼不比對**（手冊不記錄密碼，見步驟 11）；Odoo 以外產品的後端帳號也比對不到，由下面的確認處理。
- `profiles-*.json` 不含憑證，可以附在工單上；Odoo 服務的紀錄含後端網址、資料庫與使用者名稱（不含密碼）。

**第三段：逐一確認每個帳號的後端帳號（步驟 10 的前提）**。還原會把每個保留帳號的後端連線換回備份當時的帳號與密碼。在這裡沒確認好就發 token，新 token 的持有人會用備份當時的後端帳號讀寫資料，例如備份之後已經換成受限使用者、或換到另一個資料庫的帳號：
- 用管理頁的 Profile 選單逐一切到每個要保留的帳號（含預設帳號與核對時重建的帳號），打開 Connection 頁，看網址、資料庫、使用者這類不含密碼的欄位。
- 和步驟 1 負責人的說明對照（Odoo 也可以對照 `profiles-before.json` 的後端帳號；第二段已經比對過，`[後端]` 都處理了才會印 `RESULT: OK`）。其他產品沒有這份紀錄，這裡是唯一的檢查：負責人說過備份之後換過的帳號，一定要逐一看。
- 不對的在 Connection 頁改回負責人說明的後端帳號，密碼由負責人提供；改了什麼記在工單上，改完再跑一次第二段。
- 還原帶回來的帳號 token 都是撤銷狀態，在 Connection 頁改後端帳號不會讓它重新可用。
- 每個保留的帳號都確認過（記在工單上），才可以做步驟 10。

### 10. 重新發放 MCP token

先確認步驟 9 都做完了：第二段最後一行是 `RESULT: OK`（或只剩負責人決定保留、記在工單上的差異），第三段每個保留帳號的後端帳號都確認過。

在 Tokens 頁，對**步驟 9 核對後留下的每一個 profile** 各自發新 token，包括核對時重建的帳號。不要對步驟 9 決定刪除的帳號發 token（應該已經刪掉了）。完整網址以帶外方式交給使用者，並請使用者更新 Claude.ai、Claude Code、n8n 等客戶端裡的連線網址。

不要把 token 寫進工單、聊天或任何 log。

### 11. 後端密碼

restore guard 只處理 MCP 閘道自己的憑證，**不會撤銷後端帳密**（Odoo 帳號密碼／API key、n8n API key、LiteLLM key 等）。每個 profile 的後端連線（含帳密）也會回到備份時的值；後端帳號（連哪個使用者、資料庫、網址）已在步驟 9 第三段確認，這一步只處理密碼。

- 如果後端資料（例如 Odoo 資料庫）也一起還原了，舊的後端密碼可能重新有效。請依後端的身分系統輪替。
- Odoo 請用正常的介面或 RPC 改密碼或 API key：隔離實驗顯示正常 RPC 修改後，所有 worker 都拒絕舊密碼。不要直接改資料庫，否則 worker 的憑證快取可能還接受舊密碼。
- 輪替後，在 MCP 管理頁的 Connection 頁更新各 profile 的後端密碼。只換密碼、後端帳號不變時，Odoo 的 MCP 網址照舊有效（換了後端帳號才會換 token）。

### 12. 驗證

- 備份裡的 token、還原前在用的 token：全部 403。
- 舊管理密碼：401。
- 每個 profile 的新 token：可以列出工具，並實際呼叫一個唯讀工具，只看到自己後端帳號的資料。
- 跨 profile 的 session 不可共用。
- 帳號清單、工具政策（Tools、Permissions 頁）、寫入開關與 Reviewed Odoo methods（Settings 頁）、Odoo 的後端帳號與步驟 9 核對後的結果相同（可以重跑步驟 9 的第二段，這時只會多出 `[token]`，因為步驟 10 已經發了 token）。也就是和步驟 1 的紀錄相同，只有負責人決定保留、記在工單上的差異例外。它們不一定和備份時相同，備份之後加的限制要留著。停用的工具不出現在工具清單，硬呼叫也被拒絕。
- 其他產品的後端帳號：和步驟 9 第三段確認的結果相同（第二段比對不到這一項）。
- 步驟 9 刪掉的帳號：管理頁上找不到。

### 13. 清理

```bash
$K delete job/mcp-restore-inplace --ignore-not-found --timeout=120s
$K delete pvc/${REL}-restore-source --timeout=120s        # 之後請叢集運維確認對應的 Longhorn 卷已刪除；PVC 仍被 Pod 掛載時會卡在 Terminating，--timeout 讓指令在 120 秒後結束並回報逾時，不是失敗，先找出還在用它的 Pod（D4）
$K delete configmap/mcp-restore-preflight --ignore-not-found --timeout=120s   # 步驟 1 前置檢查用（D3）
```

- 前置檢查的 Job（`mcp-restore-preflight-image`、`mcp-restore-preflight-source`）已在步驟 1 刪除；如果中途停止而沒刪，一併 `$K delete job/<名稱> --ignore-not-found --timeout=120s`。

- live 卷上的 `.mcp-restore-inplace` 工作目錄與還原前資料（內含當時有效的 token）已由工具在成功時刪除。
- 工作目錄裡的 `profile-inventory.py`、`profiles-*.json`、`preflight.py`、`restore-job.yaml` 都不含憑證，可以附在工單上或刪除（Odoo 服務的 `profiles-*.json` 含後端網址、資料庫與使用者名稱）。
- `reset-config` 會留下 `.config.json.reset-backup-<32 位 hex>`（內含重設前的舊設定與舊憑證），見下方「已知問題」。這個檔案在 live 卷上時，還原會把它與其他舊資料一起移開並刪除；在備份來源上時，guard 會略過它。它不會被還原回 live。
- 步驟 2 的 snapshot 含還原前有效的 token，確認不需要回退後，請叢集運維刪除。

## 這個程序保護不到的情況（剩餘風險）

- **繞過程序**：
  - 直接回滾 live 卷（Longhorn snapshot revert、把舊卷換回、用 `kubectl cp` 放回舊檔案）；
  - 用舊映像啟動還原後的資料；
  - 有叢集權限的人刻意這麼做。
  以上都會讓舊 token 復活。要連這些情況都擋住，需要不隨卷回滾的外部撤銷權威，屬於另案。
- **後端密碼**：見步驟 11，guard 不處理。
- **還原會把帳號、政策與後端連線也帶回備份時（多帳號實測 X1）**：
  - 會回到備份當時的有：帳號清單、各帳號的名稱、後端連線（後端帳號與密碼）、工具政策、寫入開關、Reviewed Odoo methods；
  - 只有 MCP 憑證會重設：token、管理密碼、管理 session generation、n8n 子程序 bearer。
  - 步驟 1 的紀錄與步驟 9 的比對可以補回帳號、名稱、工具政策、開關與 Reviewed Odoo methods，也會比對 Odoo 的後端帳號。core 或 app 新增每帳號政策欄位時要同步 `profile-inventory.py`：`tests/test_restore_runbook.py` 列舉經 `store.patch` 寫進 `mcp_server`、`tools` 的欄位，沒有歸類就失敗。
  - 比對不到、要靠人的有：Odoo 以外產品的後端帳號（步驟 9 第三段向負責人逐一確認）；後端密碼（不記錄，見步驟 11）；步驟 1 讀不到的狀態（服務沒有在跑時，只能靠負責人提供）；不是權限的設定，例如 n8n 的 session 上限與逾時，也會回到備份當時。
  - 跳過步驟 9 直接發 token：備份之後刪掉的帳號會重新可用，備份之後加的限制會消失，新 token 也會用備份當時的後端帳號讀寫資料。舊 token 不會復活，問題出在新發的 token。
- **備份之後才發生的操作紀錄**不在備份裡，無法還原。工具會把舊操作 ID 的有效期限設到最新的已知時間點，管理頁對這些舊 ID 會回 410，請重新讀取狀態後再操作。
- **n8n** 的 catalog 資料庫必須是已 checkpoint、沒有 WAL／SHM 的一致快照，否則工具會拒絕。
- 備份檔、snapshot 與 restore-source 卷都含敏感資料（舊 token、後端連線資訊），要依敏感資料保管。

## 已知問題與後續工作

以下以審查編號標明來源。

- **reset-config 的 reset-backup 殘留（restore-1、D2 P2-2）**：平台 operator 的 `reset-config` 會先把舊 `config.json` 寫成設定卷根目錄的 `.config.json.reset-backup-<uuid4().hex>`（32 位小寫 hex），之後才改寫 `config.json`。它的清理 exec 可能失敗（演練三輪都 exit 1，因為清理時舊 Pod 已在終止），檔案就留在卷上，內含舊的明文管理密碼；之後對這顆卷做的備份也會含這個檔。
  - guard 現在會略過來源**根目錄**下「名稱完全符合 `.config.json.reset-backup-` 加 32 位小寫 hex、且 `lstat` 為一般檔案」的項目：不讀、不複製、不發佈，也不計入驗證，處理方式與 `lost+found` 相同，所以還原後 live 卷不會有來源的這個檔。
  - 名稱相近但不符（大小寫、長度、非 hex、多了副檔名或結尾字元），或名稱相符但不是一般檔案（symlink、目錄、FIFO），仍會被拒絕。profile 子目錄裡的同名檔也仍會被拒絕（operator 只寫在根目錄）。
  - 這個檔是舊 `config.json` 的備份，**不可手動複製回 live**。live 卷上原有的這個檔，會在還原的 `move_aside` 被移開、`discard_live` 被刪除。
  - 需要映像含這項修正（見前提）。
  - 後續工作：operator 的清理要修正（例如對「目前存在的所有舊檔」而不是單一 Pod 清理，或不把備份寫在設定卷上）。這屬於平台 repo，不在本分支範圍。
- **Headscale 的 `admin-sessions.sqlite3`（2026-11-13 多帳號實測 P1）**：Headscale 管理程式啟動時沒有就會在設定卷根目錄建立 `admin-sessions.sqlite3`（每次登入的撤銷紀錄與隨機 epoch，不是憑證），所以 Headscale 卷的備份都有它。修正前的 guard 不認得這個檔，照本手冊還原 Headscale 一律在備份檢查與步驟 5 的 `prepare` 停下（不會讓 token 復活，也不會毀損資料）。
  - guard 現在會略過來源**根目錄**下名稱完全是 `admin-sessions.sqlite3`、`admin-sessions.sqlite3-journal`、`admin-sessions.sqlite3-wal`、`admin-sessions.sqlite3-shm`，且 `lstat` 為一般檔案的項目：不讀、不複製、不發佈。名稱相近、不是一般檔案，或出現在 profile 子目錄，仍會被拒絕。
  - 還原後 live 卷上沒有這個檔；服務在步驟 7 啟動時建立新的空資料庫（新的 epoch）。所以還原前的所有管理登入，包括備份之後才登出的，都不會再被接受（管理密碼與 `admin_session_generation` 也已換掉）；步驟 8 重設密碼後重新登入即可。live 卷上原有的這個檔隨其他舊資料在 `move_aside` 被移開、`discard_live` 刪除。**不要把備份裡的這個檔手動放回 live 卷**。
  - 第二段備份檢查通過時，`unexpected_entries` 仍會列出 `admin-sessions.sqlite3`（與 reset-backup 一樣只是列出，guard 會略過），這是正常的。Headscale 的每份備份都有這個檔，所以 `source` 模式回 exit 1 時，`unexpected_entries` 列出它（或再加上上述三種附屬檔）**不能**用來推論映像缺這項修正：在含修正的映像上，帳號 port 不符、hard link、該檔是 symlink 等其他拒絕原因，輸出也一樣會列出它（2026-11-13 審查實測）。要判斷映像有沒有這項修正，照上方「辨識方式」以 `$IMAGE` 的 digest 對照建置紀錄（必須是含 woow-mcp-server #146 的建置或之後的映像），不要憑 tag 或拒絕輸出推論；確認映像含修正之後仍被拒絕，就依其他拒絕原因檢查備份內容。不論哪一種情況，都不要刪備份卷上的檔案。
  - 需要映像含這項修正（見前提）。正式站 Headscale（hs324）換上含修正的映像之前，不要照本手冊還原它。
- **還原中斷時 seed init container 可能寫入新的 `config.json`（D2 P2-1）**：chart 0.1.10 之前的 seed init container，在還原標記存在、服務 Pod 重啟（CrashLoopBackOff）時，可能在卷上寫入一份全新的 `config.json`（含 chart seed 的管理密碼）。服務仍會因標記拒絕啟動，重跑還原也會把它移開並覆蓋，所以沒有安全後果；但「卷內容不變」的說法不準確，放棄還原時要知道卷上可能多了這份檔案。chart 0.1.10 已修正：seed init container 在設定目錄有 `.mcp-restore-in-progress` 時不讀 seed、不寫任何檔、不改權限（seed 本身不改；kubelet 在使用 fsGroup 的叢集可能仍會調整既有檔案權限，見步驟 6），以 exit 0 結束，由主容器的 guard 以 exit 3 拒絕啟動（woow-paas-charts `fix/mcp-origin-default-chart-20261112` 的 `4cf67f8`）。另外，CrashLoopBackOff 期間 `endpointslices` 可能出現 1 個 not-ready 的 endpoint（步驟 4 的「應為空」只在 Pod 完全不存在時成立；not-ready 的 endpoint 不會接流量）。
- **憑證重設處理的欄位（restore-6）**：guard 在每一個 profile（含預設 profile）的 `config.json` 上，只重設下列已知欄位：
  - `admin_password`：換成新的隨機值；
  - `mcp_auth_token`：清空；
  - `mcp_auth_revoked`：設為 `true`；
  - `mcp_auth_generation`、`admin_session_generation`：換成新的隨機值；
  - `token_history`：清空；
  - n8n：`mcp_server.env.AUTH_TOKEN` 與 `proxy.bearer_token` 換成新的隨機 bearer（兩者相同、各 profile 不重複）。
  其他欄位（`connection`、`tools`、`mcp_server` 其餘欄位、`proxy` 其餘欄位、`admin_session_revocations` 等）原樣保留。**未知的頂層欄位也是原樣保留，不會被拒絕**：各產品設定檔的欄位不同，改成白名單會讓舊備份或新增欄位的產品無法還原。這代表存放在上述已知欄位之外的任何憑證（例如 `connection` 內的後端密碼）不會被重設，見步驟 11；其他 MCP token 的撤銷則由 `mcp_auth_revoked` 與 generation 保證。
- **profile 設定可能保留 restore hold 標記（D2 P3-1）**：從缺少 `owner_defaults_20261025` 標記的舊備份還原時，guard 會在每份設定的 `tools` 寫入 `restore_guard_preserve_policy_20261112` 這個 hold。預設 profile 啟動時由執行期把它換成 `owner_defaults_20261025`（`applied: restore_preserved`）；非預設 profile 的設定可能繼續保留 hold 標記而沒有被換掉。這是預期行為：工具政策與寫入開關都維持備份時的值（演練確認沒有擴權），hold 標記本身不影響行為，不需要手動清除。

## 演練紀錄

**步驟編號**：2026-11-13 起新增步驟 9（核對），原步驟 9～12 改為 10～13。以下各次演練（F11、D4、D3、D2）寫的步驟編號是當時的編號。

### M1（2026-11-13，多帳號核對與步驟 5 舊 Job，修改後的手冊字面重跑）

證據目錄：`/data/pi-agent/home/woow-sec-sweep/artifacts/mcp-security-release-20261112/mp-runbook-20261113/`。內含 `harness.py`、`bin/kubectl`（記錄用 shim）、`workdir/`（手冊要存的檔與 `profiles-*.json`）、`receipts/*.json`、`logs/runbook.log`（每個區塊的完整輸出）、`shim-log.jsonl`、`cleanup-proof.txt`。

**對象與環境**：
- 手冊：分支 `docs/restore-runbook-multi-profile-20261113` 的 `4d25713`。區塊以 `git show` 抽出，不改字，交給 bash；`restore-job.yaml` 只填佔位符。
- 映像：正式站的 odoo-mcp-admin `e0a80d6…@sha256:12ce0293…`，Pod 的 imageID 已核對。
- chart：mcp-server 0.1.10（sha256 `b8a49dd0…`），以 helm 3.13.3 真的 install（revision 1）。步驟 8 是 upgrade（revision 2），加上平台 develop `3af108d2` 的 reset-config 改寫腳本與 rollout restart。
- 沙盒：woow-k3s 全新的 `dev-sandbox-mp-runbook-20261113`，先建 ResourceQuota 與 NetworkPolicy（不准對外連線）。
- 帳號與後端：Odoo 用假後端（`.invalid`），預設帳號加 3 個額外帳號。
- shim 只在 Job 的 `create`／`apply` 加三樣：擁有者標籤、nodeSelector、imagePullPolicy Never。
- 管理頁上的動作（刪除、新增、改名、Tools、Permissions、Settings、發 token）改用管理頁呼叫的同一組管理 API。

**情境**：
- 備份前：A（預設）停用 fit_gap_report，B、C 沒有限制，D 停用 data_quality_report。
- 停寫複製備份之後：刪掉 D；新增 E（停用兩支工具）；C 停用 upgrade_risk_report 與 execute_approved_write 的 unlink；B 改名為 mp-B-renamed，並關掉 ODOO_MCP_ENABLE_WRITES。

**在叢集上驗證過**：
1. 步驟 1：第一個區塊的 `IMAGE` 等於正式站 digest。帳號清單區塊在服務 Pod 內執行，`profiles-before.json` 正確記下 4 個帳號、各自的停用工具與操作、開關。輸出與檔案比對當時所有合成憑證，0 命中。
2. 前置檢查兩個 Job 改走 `new_job` 後，依序印 `created uid=…`、`job succeeded`、`exit=0`，最後是 `image_ok`／`ok`；兩個 Job 都已刪除。
3. 步驟 4、5：新區塊建立 Job，依序印 `created uid`、`job succeeded`、`exit=0` 與完成訊息，Pod 的擁有者 uid 等於 Job uid。步驟 7 後：
   - healthz 200；
   - 5 個舊 token 的 POST、GET 都是 403，舊密碼 401；
   - 卷上的變化重現了 X1：D 以備份裡的 id 與政策回來、E 消失、C 的限制消失、B 的名稱與開關回到備份時。
4. 步驟 9 第一段：正好列出 6 項差異——B 的 `[名稱]` 與 `[開關]`、C 的 `[工具]` 與 `[操作]`、D 的 `[刪除]`、E 的 `[重建]`；還原帶回來的帳號全部是 `revoked`。
5. 核對分兩輪，每輪後跑一次步驟 9 第二段：
   - 第一輪刪掉 D，以同名重建 E，但還沒設政策。第二段列出 5 項，包括重建的 E 只停用 fit_gap_report：新帳號複製了預設帳號當下的政策。
   - 第二輪補回政策、名稱與開關後，印出 `[已重建]` 與 `RESULT: OK`。
6. 步驟 10、12：
   - 對 A、B、C、E 發新 token，都是 200；工具數 40／41／40／39；
   - 停用的工具不在清單上，硬呼叫回 isError；`list_instances` 只看到自己的後端；
   - 跨帳號 session 404；D 不在帳號清單上；舊 token 都是 403；B 的寫入開關是 0。
7. 重跑保護：還原後重跑步驟 1 的帳號清單區塊，印 `STOP`，`profiles-before.json` 沒變；重跑步驟 9 第一段，沿用 `profiles-restored.json`。
8. 對照舊區塊（`e0a80d6` 版）：上一次的還原 Job 還在時，`apply` 印 `job.batch/mcp-restore-inplace configured`。接著印 `job succeeded`、上一次 Pod 的 `exit=0` 與完成訊息，但沒有新的 Pod。步驟 7 之後，第一次還原後發的 4 個 token 仍是 200，也就是根本沒有還原。在這個狀態跑新的步驟 9 第一段，4 個帳號都被標 `[token]`。
9. 新區塊在同一個狀態：先刪掉上一次的 Job，再建立新的（新 uid、新 Pod）並跑完。步驟 7 之後，那 4 個 token 都是 403。
10. 建立失敗（ResourceQuota 的 `count/jobs.batch` 設為 0）：kubectl 印 `exceeded quota`，接著 `STOP: 建立 job/… 失敗`；沒有輪詢，也沒有任何 `job …` 或 `exit=` 行。
11. 舊 Job 刪不掉（加了 finalizer）：kubectl 先印 `deleted`，120 秒後印 `timed out waiting for the condition`，接著 `STOP: 刪不掉舊的 job/…`，沒有建立任何東西。拿掉 finalizer 後 Job 消失。
12. 步驟 13 字面執行後，restore-source PVC 與 ConfigMap 都已刪除。
13. 稽核：25 個合成憑證掃 45 個證據檔，0 命中；服務 log 也 0 命中，沒有遮罩的 `/private_` 0 行。
14. 清理：namespace 刪除後 NotFound，2 顆 PV 都 NotFound，沒有 claimRef 指向它的 PV。

**沒有在叢集上驗證**：
- 步驟 2、3（Longhorn 快照、從 Longhorn 備份還原）：改用停寫複製到 `${REL}-restore-source`。
- 真的 Odoo 後端：用假後端，路由以 `list_instances` 印出的設定網址判斷（真 Odoo 的資料隔離見 mp-verify-restore-20261113）。
- n8n、Hermes、LiteLLM、Headscale：`profile-inventory.py` 只讀各產品共用的欄位，`SWITCHES` 由單元測試比對各 app 的政策，但這次只跑了 Odoo。
- 管理頁的實際點擊：改用同一組管理 API。
- `[讀不到]`、`[多出]` 兩種標記，以及步驟 5「舊 Pod 還在」的 STOP：只有單元測試（`packages/mcp-admin-core/tests/test_restore_runbook.py`）。

**M1 之後的修改（獨立審查，2026-11-13；沒有在叢集上重跑）**：
- `profile-inventory.py` 加記 Reviewed Odoo methods（`mcp_server.odoo_allowed_methods`，`[方法]`）與 Odoo 的後端帳號（網址、資料庫、使用者，`[後端]`），`[重建]` 列也列出這兩項。審查以 M1 版的腳本重現：步驟 1 的清單只有 `sale.order.action_confirm`，還原後多了 `account.move.action_post`，token 都是 revoked，比對仍印 `RESULT: OK`、exit 0。
- 後端帳號的確認從步驟 11（發 token 之後）移到步驟 9 第三段，列為步驟 10 的前提；前提段與步驟 1 請負責人一併說明備份之後改過的後端連線。
- 驗證：單元測試以合成設定卷重現上面兩種情況（修改前失敗、修改後通過），並列舉 core 與各 app 經 `store.patch` 寫進 `mcp_server`、`tools` 的欄位，新增政策欄位沒有同步帳號清單時測試失敗。

### F11（D4 發現的修正，在叢集上以修正後的區塊重驗）

證據目錄：`/data/pi-agent/home/woow-sec-sweep/artifacts/mcp-security-release-20261112/fix-f11-restore-runbook-logs/`（`extract.py` 從本檔抽出程式區塊、`bin/kubectl` 記錄用 shim、`scn/*.sh` 各情境、`logs/*.log` 各情境完整輸出、`shim-log.jsonl`）。

**對象**：本檔修正後的版本（基於發版 `ff3dbc0`），指令區塊由 `extract.py` 從本檔原樣抽出、不改字、直接交給 bash（kubectl v1.37.1）。環境：woow-k3s 自建、帶 owner label 的 `dev-sandbox-f11-43cb90`；映像是正式站目前 pin 的 odoo MCP 映像（`odoo-mcp-admin:8297aa4…@sha256:39e87b44…`，唯讀查 `paas-ws-63ca8e57` 的 Deployment 取得）。沒有跑完整還原。

**與手冊範本的差異（drill 專用）**：PATH 前面的 `bin/kubectl` 只在 `apply` Job 時動手腳：固定 nodeSelector 到有映像快取的節點；(a) 載入發版程式 overlay（`python3 /code/boot.py` 前綴，`/code` 改成 projected ConfigMap）；(b) 換成不存在的映像 digest；(c) 以 emptyDir 取代兩個 PVC、並把 command 換成印一行後睡覺的假還原；(d) 把腳本路徑換成不存在的檔案。其他 kubectl 指令原樣轉呼叫。

**在叢集上驗證過**（D4 的 P2、P3 逐項）：
1. **(a) 正常 Job**：`wait_job` 印 `job succeeded`，`job_report` 印 `<pod> exit=0`，接著是 log（`JWT_SECRET` 警告與 `"preflight": "image_ok"` 那一行）。
2. **(b) 拉不到映像**（照字面、`activeDeadlineSeconds: 300`）：Pod 存在（ErrImagePull）時 `job_report` 印 `<pod> exit=`（空白），`timeout 60 kubectl logs <pod>` 立刻回「container is waiting to start」，函式印出看 describe 的提示；Job 因 deadline 失敗、Pod 被清掉後，`job_report` 印一行 `NO POD: …` 並立刻回傳，整段 308 秒（全是 300 秒的 deadline）。D4 在這個情況卡了 650 秒。
3. **(c) 步驟 5 執行中刪 Pod**：輪詢迴圈在約 12 秒印 `job failed`；`Failed` 條件為 `BackoffLimitExceeded`；Pod 清掉之後 `job_report` 印 `NO POD`，約 1 秒。同一狀態下舊寫法 `kubectl logs job/mcp-restore-inplace`（加 `timeout 45`）逾時（rc 124，沒有輸出）、舊的 `{.items[0]…}` jsonpath 印多行 `array index out of bounds` 錯誤（rc 1），兩個 D4 的問題都在叢集上重現。刪 Job 後可重新套用同名 Job。
4. **(d) 判讀**：舊映像跑 `preflight.py image generic`：`exit=2` 加 `{"preflight": "refused", "reason": "image_lacks_restore_support", …}`；腳本路徑錯：`exit=2`，log 只有 `python3: can't open file '/code/nonexistent.py'`，沒有任何 JSON 行，依新文字判為「其他錯誤」。舊映像跑步驟 5 的真指令：`exit=1`，log `No module named mcp_admin_core.restore_inplace`（步驟 6 新增的說明）。
5. 步驟 12 的三個刪除指令（加 `--timeout=120s`）、`wait_job` 加 `--request-timeout` 照字面可執行；所有區塊 `bash -n` 通過。
6. 清理：namespace 已刪除，沒有 PVC 殘留；但 StorageClass `longhorn` 的 PV 是 reclaimPolicy Retain，`pvc-83fa7d46-…`（我建的 1Gi 空 PVC）在 namespace 刪除後停在 `Released`，沒有自動消失（D4 的 PV 會隨即消失），Longhorn 卷需要叢集運維確認（本次沒有碰 `longhorn-system`）。

**沒有在叢集上驗證**：
- 完整還原、真的 `restore_inplace` 在卷上的行為（F11 只改文件；D4 驗證過完整還原）。
- `delete pvc --timeout=120s` 在「PVC 仍被 Pod 掛載而卡在 Terminating」時的逾時行為：只驗了沒被掛載的 PVC 正常刪除。
- `timeout 60 $K exec …`（步驟 7）：只驗語法，沒有跑（沒有 Deployment）。
- 步驟 1 的 `$K` 在 zsh 下需要 `SH_WORD_SPLIT` 的說明：由 shell 規則得出，沒有實測。
- `job_report` 在節點故障（Pod 進入 Unknown／Terminating 很久）時的輸出：沒有做；此時 `kubectl logs <pod>` 受 60 秒上限約束，不會卡住。

**F11 修改清單**（全部對應 D4）：
- P2：映像前置檢查兩處與步驟 5 的 `kubectl logs job/<名稱>` 改為 `job_report`（先 `get pods -l job-name=<名稱> -o name`，沒有 Pod 就印 `NO POD` 並指示看 `describe job/` 與 events；有 Pod 才對每個 Pod `timeout 60 $K logs`）。
- P3：讀 exit code 改用 `{range .items[*]}`，輸出 `<pod> exit=<數字或空白>`，沒有 Pod 時由 `NO POD` 訊息取代；步驟 6「Job 被外力殺掉」的描述與實際輸出一致。
- P3：「exit 2 是映像不支援」改為「exit 2 且有 `image_lacks_restore_support` 的 JSON 行才是」；表格的「其他錯誤」列明確包含 exit 2 但沒有 JSON 行的情況（python 找不到腳本）。
- 其他可能無限等待的指令（D4 要求逐一檢查）：`kubectl delete`（預設等到物件消失，PVC 被掛載時會卡在 Terminating）全部加 `--timeout=120s`（步驟 1 的 preflight Job 與 ConfigMap、步驟 6 刪 Job、步驟 12 與其下的 `--ignore-not-found` 說明）；`kubectl get job/…` 輪詢與 `get pods` 加 `--request-timeout`；步驟 7 的 `kubectl exec` 加 `timeout 60`。`kubectl wait`、`rollout status` 本來就有 `--timeout`，兩個輪詢迴圈有次數上限（72、372 次），沒有 `logs -f`、`attach`、無上限迴圈。
- 補上 `$K` 是變數（不是 alias／function）與 bash 假設的說明。
- 步驟 6 補「exit 1 但 log 是 No module named」的判讀。

### D4（ff3dbc0 加真 chart，手冊字面重跑）

證據目錄：`/data/pi-agent/home/woow-sec-sweep/artifacts/mcp-security-release-20261112/d4-runbook-literal/`（`d4.py`、`bin/kubectl`、`run-all.py`、各階段 receipt、`shim-log.jsonl`、`logs/run-all-2.log`、`cleanup.json`、`evidence-secret-scan*.json`、`report-zh-TW.md`、`result.json`）。

**對象**：發版 commit `ff3dbc0748c2e4aae4126a57b2cf83f1d8cc4249`（`git archive` 的 overlay，tar.gz sha256 `10e94269…`）與 woow-paas-charts `4cf67f8`（mcp-server 0.1.10，`helm template` 渲染、預設 Origin 設定）；映像為正式站 pin 的 odoo-mcp-admin（8297aa4，digest 39e87b44…），真的 Odoo 18 + PostgreSQL、兩個 profile；全新隔離 namespace。所有手冊程式區塊自 git 抽出、不改字、交給 bash，只有 PATH 前的 kubectl shim 在 Job `apply` 時加 owner label、nodeSelector、overlay 與各情境變化。

**在叢集上驗證過**：步驟 1 的 `IMAGE`（單行、多行、零行、空行加合法行）；映像前置檢查三種情況（overlay 支援 exit 0 `image_ok`、舊映像 exit 2 `image_lacks_restore_support`、其他錯誤：錯誤腳本路徑 exit 2 無 JSON、壞參數 exit 64、source 模式沒掛來源 exit 1、拉不到映像）與備份來源檢查（reset-backup 只列在 `unexpected_entries`）；步驟 4（正常 scale down、忽略 SIGTERM 的 Pod 讓 `kubectl wait` 逾時）；步驟 5、6（在 `move_aside` 後刪 Pod、輪詢印 `job failed`、`Failed` 條件 `BackoffLimitExceeded`、同名 Job 未刪就 apply 為 unchanged、刪 Job 後重跑 exit 0）；完整還原（舊 token、舊 session、舊管理密碼全部失效，reset-backup 沒回到 live，舊 operation 重放 410，政策與寫入開關維持備份時的值，reset-config 與 token 重發後 tools/list 39 個、只看到自己的資料，跨 session 不通）；步驟 7、12。清理後兩個 namespace 與 6 顆 PV 都確認刪除。

**發現**（已由 F11 修正，見上）：P2 `kubectl logs job/<名稱>` 在 Job 沒有 Pod 時卡住（拉不到映像約 650 秒、步驟 5 刪 Pod 後 305 秒以上，皆手動中止）；P3 沒有 Pod 時 jsonpath `.items[0]` 印多行錯誤而不是空白；P3 python 找不到腳本也是 exit 2，與「exit 2 是映像不支援」同號。D3 的 P3 全部解決；D3 的 P2（operator reset-config 清理，平台 repo）仍待修。

**D4 沒涵蓋**：D3 已涵蓋的 CrashLoop 標記拒絕與 seed 不寫入比對；r2-sec-4（缺 restore-1 修正的映像在 `prepare` 以 exit 1 拒絕）；Longhorn snapshot／備份還原、真 operator reset-config（以等價腳本模擬）、n8n 等其他產品。

### D3（最終發版 commit 加真 chart）

證據目錄：`/data/pi-agent/home/woow-sec-sweep/artifacts/mcp-security-release-20261112/d3-restore-final/`（`d3.py`、`mcp.py`、`run-all.py`、各階段 receipt、`logs/run-all-{1,2,3}.log`、`chart-render-redacted.yaml`、`cleanup.json`、`evidence-secret-scan*.json`、`report-zh-TW.md`）。

**對象**：發版 commit `ec82641de63c85f83b4bef4dbc59f79970c52065`（以 `git archive` 的 commit 物件產生程式 overlay）與 woow-paas-charts `4cf67f8`（mcp-server 0.1.10）。Deployment、Secret、ConfigMap、PVC 由 chart 以 `helm template`（v3.19.5）渲染，再 `kubectl create`（含 seed-config init container）；還原 Job 照本手冊 ec82641 版的範本**字面套用，只替換佔位符**；步驟 1、4、5、7、12 的程式區塊與 preflight 以 `git show` 抽取後逐字執行。映像是正式站 pin 的 odoo-mcp-admin（8297aa4，digest 39e87b44…），所以新程式靠 overlay 載入到 `/tmp/code`（pod 內確認 10 個模組檔 sha256 與 git blob 相同、載入路徑在 `/tmp/code`）。環境：woow-k3s 全新隔離 namespace，真的 Odoo 18 + PostgreSQL、兩個 profile。

**在叢集上驗證過**（7 個情境，全部達成預期）：
1. 設定後 reset-config（D2 模擬）在卷上留下 `.config.json.reset-backup-<hex>`，停寫備份含此檔；備份檢查 exit 0，該檔只列在 `unexpected_entries`，guard 略過（restore-1 第一次在叢集上以 chart 渲染的 Deployment 驗證）。
2. 備份後 A 輪替、B 撤銷、政策改變；舊 token 全 403、新 token 可用。
3. 中斷：Job 在 `move_aside` 後被殺。chart pod 的 seed init 以 exit 0 記錄「restore in progress: seed skipped, nothing written」，主容器 CrashLoopBackOff（exit 3、有拒絕日誌、沒有 Uvicorn running），舊 token、healthz、舊密碼全無回應。卷清單比對：13 筆只差 mode（kubelet fsGroup 所致），以無作用控制 pod 正規化後前後完全相同。重跑同名 Job（含步驟 5 的輪詢迴圈）exit 0。
4. 還原後：備份中的 reset-backup 沒被還原到 live；備份內與還原前的 token 全 403、舊 session 401、舊管理密碼 401、舊 operation ID 410、政策維持備份時的值、寫入開關 0；reset-config 後新密碼 200；token 重發後 tools/list 39 個並實際呼叫 `search_records` 只看自己的資料；跨 profile session 兩向 404。
5. 對照：全新空 PVC 上 0.1.10 seed 寫入 config.json，與 0.1.9 同輸入 sha256 完全相同；標記為檔案、目錄、懸空 symlink 時 0.1.10 都不寫（0.1.9 仍會寫）。
6. 步驟 1 的 imageID（`repo@sha256`）、輪詢迴圈、步驟 7、步驟 12 照字面可執行，結果與手冊敘述相符；舊映像跑 preflight exit 2、`image_lacks_restore_support`。
7. 清理：namespace 與 3 顆 PV 已確認刪除；16 個合成憑證值掃過 45 份 receipt 與 4 組日誌，0 命中。

**與手冊範本的差異**：command 前綴 `boot.py`（overlay）、`/code` projected ConfigMap、nodeSelector、owner label；Longhorn snapshot／備份還原以停寫檔案複製取代；步驟 8 為等價模擬；chart NetworkPolicy 關閉；映像 tag@digest；以 `helm template` + `kubectl create` 取代 `helm install`。

**D3 發現並已回寫手冊的項目**（本次修訂）：步驟 1 沒設定 `IMAGE`（r2co-5）；映像檢查沒有 Job 範本，映像支援時只看到 `FileNotFoundError` traceback（改為 `image`／`source` 兩個模式與完整 Job 範本）；步驟 4 沒有等待指令；步驟 6 沒有刪 Job 的指令、Pod 被殺時無 exit code、Job 保留到步驟 12；Job 日誌多一行 `JWT_SECRET` 警告；fsGroup 使 mode 改變。另 reset-config 的 operator 清理（D2 P2-2，平台 repo）仍待修，見「已知問題」。

**D3 沒涵蓋的部分**：Longhorn snapshot／備份還原與回退；真實 operator 的 reset-config；其他產品（n8n、Headscale、Hermes、LiteLLM）的還原；舊格式與缺標記備份回合（D2 做過）。**本次修訂後新寫的內容沒有在叢集上跑過**：`image`／`source` 兩個 preflight 模式與兩個 preflight Job 範本、步驟 1 設定 `IMAGE` 的指令（只以假值在本機 shell 驗證）、步驟 4 的 `kubectl wait`；preflight 的 Python 程式只在本機以發版 venv 驗證過三種情況（映像支援、映像不支援、其他錯誤）與備份來源檢查。「映像缺 restore-1 修正時 prepare 以 exit 1 拒絕」只由單元與整合測試涵蓋，不是叢集驗證（r2-sec-4）。

### D2

證據目錄：`/data/pi-agent/home/woow-sec-sweep/artifacts/mcp-security-release-20261112/d2-restore-drill-final/`（腳本、各階段 receipt、`logs/run-all-1.log`、`logs/run-all-2.log`、`cleanup.json`、`evidence-secret-scan.json`、`report-zh-TW.md`）。這是對最終發版 commit 385da83 的完整演練；先前的 `b-origin-log-restore` 只是較早的簡版。

**做過的情境**（woow-k3s 全新隔離 namespace，真的 Odoo 18 + PostgreSQL、兩個 profile、各綁受限 Odoo 使用者）：
- 新格式備份：還原後，備份內與還原前的所有 token 全部 403、舊管理密碼與舊 session 401、舊 operation ID 回 410、工具政策維持備份時的值；以等價於 operator `reset-config` 的方式重設管理密碼後新密碼 200、舊密碼 401；兩個 profile 重發 token，tools/list 並實際呼叫唯讀工具，只看到各自的資料；跨 profile session 兩個方向都不通。
- 在「標記已寫入、舊資料已移開」之後殺掉 Job：服務 CrashLoopBackOff（exit 3、沒有 ready endpoint），所有舊 token 與舊密碼都沒有回應；重跑同一個 Job 後 exit 0。
- 舊格式備份、缺少標記的舊備份：preflight ok、Job exit 0、舊 token／密碼全部失效、政策沒有擴權、寫入開關維持關閉。
- 舊映像（8297aa4）跑 preflight：exit 2、`image_lacks_restore_support`。
- 洩漏稽核：23 個合成憑證值掃過 53 份 receipt 與 4 組日誌，0 命中。namespace 與 4 顆 PV 已確認刪除。

**沒有涵蓋的部分**：
- **Longhorn snapshot／備份還原路徑**：步驟 2、3 以「停寫後的檔案複製到第二顆 PVC」取代（沒有碰 `longhorn-system`），沒有演練 snapshot 回退，也沒有演練由 Longhorn 備份還原成新卷；備份 PVC 名稱不是 `${REL}-restore-source`。
- **n8n、Headscale、Hermes、LiteLLM 沒有演練**，只有 Odoo。n8n 的流程只由單元／整合測試涵蓋。
- **Job 範本沒有以原樣執行**：演練的 Job 加了 serviceAccount、nodeSelector、`imagePullPolicy: Never`、`/tmp` emptyDir、`readOnlyRootFilesystem`、以 ConfigMap 掛入程式碼；演練也沒有以 `repo@sha256` 固定映像（restore-4）與輪詢等待迴圈（restore-5），這兩處是審查後才改的，沒有在叢集上跑過。（D3 已在叢集上補驗這兩處，見上方 D3 紀錄。）
- 步驟 8 是等價模擬（Secret 更新、exec 改 `admin_password`、rollout restart），不是真的經 operator／helm；步驟 7、9、11 用叢集內 probe 執行。
- 中斷測試只做在新格式備份那一輪。
- reset-backup 檔被 guard 略過（restore-1）：演練時只由讀程式得出結論；修正以單元測試與整合測試驗證，沒有在叢集上以真實 operator 產生的檔案演練。
- 這是合成資料的隔離實驗，不是正式平台的還原驗收。
