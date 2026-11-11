# K8s MCP Admin

Admin bundle（Web GUI + built-in MCP reverse proxy + process manager）打包上游
[containers/kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server)
**v0.0.65**（Go binary、原生 Streamable HTTP），比照 `apps/odoo-manage` 打包進本 monorepo，
產出 image `k8s-mcp-admin`。

架構分層與其他 app 一致：admin 後端 **不呼叫 k8s API、不引入 kubernetes python client、不做叢集
資源管理或自我 rollout**，只管理自己 spawn 的子行程；連叢集做認證操作的是子行程
`kubernetes-mcp-server`（內建 client-go）。admin 只管檔案（config.json）與行程。

## 概述

- 子行程 = 上游 Go binary `kubernetes-mcp-server`（釘 **v0.0.65**、sha256 釘死、build-time 烙進
  image，runtime 零下載）。由 launcher console script `k8s-mcp-http` 以
  `--port 8000 --bind-address 127.0.0.1` 起（`K8S_MCP_MULTI_CLUSTER` 關閉時再加 `--disable-multi-cluster`）。
- **transport**：唯一支援 **Streamable HTTP**，經 core proxy 直轉
  `/private_<token>/mcp`。`/sse` 標為 caveat（見下）。
- **工具數**：預設開 `config,core,helm,kcp,kubevirt,tekton` 六個工具集（上游 v0.0.65 不需另外設定就能用的
  全部工具集），共 **34 支**：`core,config,helm` 23 支，另加 kcp 2 支、KubeVirt 4 支、Tekton 5 支（要叢集有裝
  對應元件才有作用）。多叢集模式（預設開）下 kubeconfig 有多個 context 時再多 `configuration_contexts_list`
  （**35 支**），其他 31 支多一個 `context` 參數；只有一個 context 時與單叢集相同。（`targets_list` 只給非
  kubeconfig 的 provider，這裡不會出現。）`kiali` 要設定 Kiali 網址、`netobserv` 的預設網址只對 MCP 所在叢集有效，
  兩者不開。此數字錨定 v0.0.65，升版須重新對帳。
- **連線模型**：connection 僅 `kubeconfig`（多行 secret textarea）＋ `in_cluster`
  （字串 `"true"`/`"false"`）兩欄；`env_prefix="K8S_MCP_"`，鍵注入子行程成
  `K8S_MCP_KUBECONFIG` / `K8S_MCP_IN_CLUSTER`。

### `/sse` caveat（未端到端驗證）

上游同埠同時掛 Streamable HTTP（`/mcp`）與 SSE（`/sse` + `/message`）。endpoint 事件回傳的
message 路徑原本會掉 `/private_<token>` 前綴；core proxy 現在會替 `event: endpoint` 補上前綴
（root-relative 或指回 loopback 的絕對網址都處理，見 `mcp_admin_core/tool_filter.py`
`prefix_endpoint`），但 k8s 的 `/sse` 沒有做端到端驗證。**請一律使用 `/private_<token>/mcp`
（Streamable HTTP）**。

## 部署時 seed（不在 repo）

正式部署由 PaaS 平台（paas-charts mcp-server chart）提供 `config.json` seed（**非 repo 程式碼**）。
預設全開（負責人 2026-10-25「預設全開」）；標準 kubeconfig 模式：

```json
{
  "connection": {
    "kubeconfig": "<kubeconfig YAML 全文（多行字串）>",
    "in_cluster": "false"
  },
  "mcp_server": {
    "command": "k8s-mcp-http",
    "args": ["--port", "8000"],
    "port": 8000,
    "env": {
      "K8S_MCP_READ_ONLY": "0",
      "K8S_MCP_DISABLE_DESTRUCTIVE": "0",
      "K8S_MCP_ALLOW_SECRETS": "1",
      "K8S_MCP_TOOLSETS": "config,core,helm,kcp,kubevirt,tekton",
      "K8S_MCP_MULTI_CLUSTER": "1"
    },
    "env_prefix": "K8S_MCP_"
  },
  "proxy": { "timeout": 86400 }
}
```

- connection 值一律字串（`_build_subprocess_env` 會 `str()` 化）。
- `mcp_server.env` 鍵**不加前綴**（注入時原樣、疊最後），故三顆治理 toggle 直接寫全名。
- `mcp_server.port`（8000）必須等於 launcher 的 `--port`（proxy 轉發目標），並避開 admin 的 8080。
- `--bind-address` **不進 seed**：launcher 硬編 `127.0.0.1`（安全邊界，見治理章節）。
- 未 seed `command` 時 admin 仍正常提供 GUI 與 `/healthz`，只是不啟動 MCP 子行程（CI smoke 依賴此行為）。
- `/healthz` **沒有** `backend` 欄位（第五輪 2026-10-27）：k8s 不宣告後端連線欄位。沒有 kubeconfig、又不在叢集內時，依上游 v0.0.65 原始碼會退回 kubeconfig provider、找不到任何 context 就啟動失敗（子行程結束，`/healthz` 的 `mcp` 是 `not_running`，Logs 頁有 launcher 印的「尚未設定 kubeconfig」）——這是「缺了起不來」，不是「起得來但沒接後端」；而且是「kubeconfig 或 in-cluster」的條件式需求，`LaunchPolicy` 的 (鍵, 標籤) 宣告表達不了（in_cluster=true 的自架部署會被誤報）。
- 沒有部署 seed、由 admin 自己建立 `config.json` 時（例如 docker compose），同一組全開的 env 由
  `LaunchPolicy.fresh_env`（`launcher.FRESH_ENV`）寫入。
- **既有 `config.json`**（2026-10-25 之前建立、沒有 `tools.owner_defaults_20261025` 標記）：第一次以新映像啟動時，
  core 的 `owner_defaults` 一次性把上面這組 env 寫進去、清空 `tools.disabled`／`disabled_operations`，並寫上標記
  （憑證、連線、token 不動，記一行 log、Settings 頁顯示已套用）。之後管理員在 Settings／Tools 頁的限制跨重啟、
  升級都保留。已有標記卻缺鍵時 launcher 維持改版前的行為（擋 Secret、`core,config,helm`、單叢集）。

**in-cluster 變體**（⚠️ **PaaS 上不可用**）：改用 Pod 自身的 ServiceAccount 連宿主叢集。PaaS 部署的 Pod
`automountServiceAccountToken=false`（租戶不該拿到平台叢集權限），`/var/run/secrets/kubernetes.io/serviceaccount/token`
不存在——此時 `PUT /api/config/connection` 設 `in_cluster=true` 會回 400、Test／Dashboard 顯示「此環境未掛載叢集權杖…」、
launcher 印同一段原因後 exit 1（#1352）。只有自行部署且有掛 SA token 時才用，connection 改為：

```json
"connection": { "kubeconfig": "", "in_cluster": "true" }
```

其餘同上。

> ⚠️ **`K8S_MCP_TOOLSETS` 屬進階操作**：seed 固定 `config,core,helm,kcp,kubevirt,tekton`、UI 不曝露。徒手改它
> **拿掉 `core` 會使 Test Connection 失效**（Test 走 `namespaces_list`，屬 core toolset）；加上 registry 沒有的
> 工具集（kiali／netobserv）則 Tools 頁管不到那些工具。非必要勿動。

## 外部叢集授權 SOP（主場景）

**PaaS 起本 service 管理「外部」叢集**（2026-07-16 裁決的主示範模式）。部署 yaml **不帶**
ServiceAccount / RBAC；叢集憑證授權在**外部叢集側**完成，再把組好的 kubeconfig 經 admin UI
貼入（或由 seed 注入 `connection.kubeconfig`）。

在**外部叢集**上執行：

```bash
# 1. 建 ServiceAccount
kubectl create serviceaccount mcp-reader -n default

# 2. 綁 RBAC——MCP 本身預設全開，AI 能做什麼由這裡決定。只想讀就綁內建 view ClusterRole（唯讀）；
#    要讓 AI 寫入／刪除／讀 Secret，就綁對應權限的 Role／ClusterRole（例如 edit、admin）
kubectl create clusterrolebinding mcp-reader-view \
  --clusterrole=view \
  --serviceaccount=default:mcp-reader

# 3. 產 token（--duration 控時效；長效 token 改建 kubernetes.io/service-account-token 型 Secret）
TOKEN=$(kubectl create token mcp-reader -n default --duration=8760h)

# 4. 取 server URL 與 CA data（沿用 current-context）
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl config view --minify --flatten -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
```

用上述三項（`server` / CA data / token）組出一份 kubeconfig：

```bash
# 5. 組 kubeconfig（貼進 admin UI 的 Kubeconfig 欄）
cat <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
- name: target
  cluster:
    server: ${SERVER}
    certificate-authority-data: ${CA}
users:
- name: mcp-reader
  user:
    token: ${TOKEN}
contexts:
- name: mcp-reader@target
  context:
    cluster: target
    user: mcp-reader
    namespace: default
current-context: mcp-reader@target
KUBECONFIG
```

於 admin UI 的 **Connection** 頁貼入 kubeconfig textarea、In-Cluster 維持 `false` → **Save** → **Test**。
（PaaS 上一定要走這條路：PaaS 部署不掛載叢集權杖，In-Cluster 模式不可用。）

### 權限雙層（兩層各自收緊）

| 層 | 邊界 | 由誰強制 |
|---|---|---|
| 外部叢集 RBAC（SA 綁的 Role/ClusterRole） | **硬邊界** | 外部叢集 API server |
| app 治理三 toggle（Read-only / Disable Destructive / Allow Secrets） | **軟邊界**（工具曝露層） | 本 app launcher → 子行程 |

治理 toggle 預設全開（不唯讀、不擋破壞性、可讀 Secret、多叢集），這時 RBAC 就是唯一的邊界；反之 RBAC 給了
cluster-admin，治理 toggle 仍能把危險工具擋在 MCP client 之外。**兩層獨立，要限制就收緊其中一層。**

> in-cluster 變體（僅管**宿主**叢集時適用）需在宿主叢集建 SA + RBAC 並掛進 Pod；正式 RBAC 樣板
> 歸 paas-charts，本 repo 的 `k8s-deploy.yaml` 只留註解型提點。

## 危險工具治理（三層縱深）

預設**全開**（負責人 2026-10-25「預設全開」，見 seed 預設）：`READ_ONLY=0`、`DISABLE_DESTRUCTIVE=0`、
`ALLOW_SECRETS=1`、六個免設定工具集 34 支、多叢集開。2026-10-25 之前開通的服務（當時的 seed 是全鎖：
`READ_ONLY=1`、`DISABLE_DESTRUCTIVE=1`、Secret 封鎖、`core,config,helm`）在第一次以新映像啟動時一次性改成全開；
之後要收緊請在 Settings 頁切換。

1. **四顆 settingsEnvToggles**（Settings → MCP Server，真 enforcement）：
   - `K8S_MCP_READ_ONLY`：唯讀模式，僅曝露 `readOnlyHint=true` 的工具（六個工具集 **19 支**；
     `core,config,helm` 為 15 支）。
   - `K8S_MCP_DISABLE_DESTRUCTIVE`：隱藏 `destructiveHint=true` 的工具（上游實測六個工具集 **9 支**，
     `core,config,helm` 為 6 支，見下方警語）。
   - `K8S_MCP_ALLOW_SECRETS`：開啟即移除 launcher TOML 的 `denied_resources` Secret 封鎖
     （預設開啟；關閉＝擋讀叢集 Secret）。
   - `K8S_MCP_MULTI_CLUSTER`：多叢集（預設開啟）；關閉＝launcher 帶 `--disable-multi-cluster`，一律用 current-context。
     關閉時工具沒有 `context` 參數；用戶端若照樣帶 `context`（例如記著多叢集時的 schema），上游 v0.0.65 會**靜默忽略、
     以 current-context 執行**，不回錯誤。這是上游行為，不是本 repo 的閘道（proxy 對 k8s 的 tools/call 原樣轉發，k8s 的
     registry 沒有參數正規化）：`--disable-multi-cluster` 把 `ClusterProviderStrategy` 設成 `disabled`
     （`pkg/kubernetes-mcp-server/cmd/root.go` 的 `loadFlags`）→ `singleClusterProvider`
     （`pkg/kubernetes/provider_single.go`，`GetTargetParameterName()`／`GetDefaultTarget()` 都回 `""`）→ 工具 handler 以
     `toolCallRequest.GetString(s.p.GetTargetParameterName(), s.p.GetDefaultTarget())` 取目標（`pkg/mcp/tools_gosdk.go`
     的 `ServerToolToGoSdkTool`），鍵是空字串、永遠讀不到 `context`；`WithTargetParameter`（`pkg/mcp/tool_mutator.go`）
     只在多目標時把 `context` 加進 schema，呼叫參數也不對 schema 驗證。因為用戶端看到的 tools/list 本來就沒有
     `context`，本 repo 不攔；要對別的叢集操作請開多叢集，或把 kubeconfig 的 current-context 改成那個叢集。

   四顆寫 `'1'`/`'0'` 進 `mcp_server.env`，切換後須 **Save + Restart** 才生效。

2. **toolsets 固定於 seed**（預設 `config,core,helm,kcp,kubevirt,tekton`、UI 不曝露）。

3. **per-tool 停用**（Tools 頁 toggle）：寫進 `config.json` 的 `tools.disabled` → `pm.restart()`
   → launcher 重讀 → 產 TOML `disabled_tools` → restart 後**真生效**。

其他固定治理：launcher TOML 顯式寫 `confirmation_fallback = "allow"`；`ALLOW_SECRETS` 關閉（或缺鍵）時
`denied_resources` 擋 `{group:"", version:"v1", kind:"Secret"}`。

> ⚠️ **Read-only / Disable Destructive 開啟時，`tools/list` 是過濾後子集**——對帳務必 mode-aware
> （見下方 smoke）。
>
> ⚠️ **`Disable Destructive` 不等於擋下全部 8 支 dangerous 工具**。app 的 dangerous 紅標（UI）
> 是 8 支，但上游 `destructiveHint=true` 實測只有 **6 支**：`pods_exec` / `pods_delete` /
> `resources_create_or_update` / `resources_delete` / `resources_scale` / `helm_uninstall`（六個工具集再加
> `vm_clone` / `vm_create` / `vm_lifecycle`，共 9 支）。
> `pods_run` 與 `helm_install` 雖屬 dangerous 紅標，但上游標 `destructiveHint=false`（Run 只
> 新建 pod、install 帶 upgrade 語意），故 `--disable-destructive` **不會**隱藏這兩支；Tekton 的
> start／restart 4 支同理（只新建 run）。`read_only=1` 會把它們一併擋掉（非唯讀）；但若只留
> `disable_destructive`、關掉 `read_only`，這些工具**仍會曝露**。dangerous 紅標（六個工具集 15 支）僅供 UI
> 紅標與 per-tool toggle；`expected_exposed_tools()` 一律只依 `destructive_hint` 值運算。

## 本機 e2e 劇本（docker-desktop / darwin）

k8s app 的 target 是叢集，無法用 `docker-compose` 一鍵拉起。本機端到端要打通**兩件事**，缺一則
Test Connection 無法真綠（實測驗證，2026-07-16）：

1. **子行程要起得來 → 必須 seed `mcp_server` 段**：admin UI 的 Connection 頁**只**寫 `connection`
   （kubeconfig），不會補 `mcp_server.command`。若 config.json 沒 seed `mcp_server`（正式環境由 PaaS
   平台注入，見上方「[部署時 seed](#部署時-seed不在-repo)」段），子行程 `kubernetes-mcp-server` **永遠不啟動**，
   Test 只能走「降級可達性檢查」而非真正的 `tools/call`。本機須自行 seed。
2. **容器要連得到叢集 API server** → 依 docker-desktop 型態二選一（見下「叢集接線」）。

### 叢集接線：兩種 docker-desktop 型態

| 型態 | 判別 | server 改寫 | TLS |
|------|------|------------|-----|
| **新版 kind-based**（現行預設） | node 名 `desktop-control-plane`、API server 走**動態埠**、憑證 SAN **不含** `kubernetes.docker.internal` | 容器 `docker network connect kind <容器>`，kubeconfig server 改 `https://desktop-control-plane:6443`（kind 私網名） | 保留原 CA（SAN 含 `desktop-control-plane` 與 node IP），**完整驗證** |
| **舊版內建 k8s** | node 名 `docker-desktop`、API server 固定 `:6443`、SAN 含 `kubernetes.docker.internal` | server 改 `https://kubernetes.docker.internal:6443` | 保留原 CA |

> ⚠️ 舊寫法「一律改 `kubernetes.docker.internal:6443`」只對舊版成立；新版 kind-based 會因埠非
> 6443、且憑證 SAN 不含該名而 `x509` / 連線失敗。新版走 kind 私網名是容器對容器直連、零宿主埠曝露、
> 完整 TLS，最乾淨。

```bash
# 1. build（Apple Silicon 必帶 --platform linux/amd64；image 為 amd64-only）
docker build --platform linux/amd64 -t local/k8s:dev -f apps/k8s/Dockerfile .

# 2. 起容器（同帶 --platform，走 Rosetta）
docker run -d --platform linux/amd64 -p 8080:8080 \
  -v k8s_mcp_data:/data -e MCP_ADMIN_CONFIG=/data/config.json \
  --name k8s-dev local/k8s:dev

# 3.（新版 kind-based）把容器接進 kind 私網，容器方能連到 desktop-control-plane:6443
docker network connect kind k8s-dev

# 4. seed mcp_server 段（見上方「部署時 seed」段）——否則子行程不啟動、Test 無法真綠。
#    連同「改寫過 server（見上表）」的 kubeconfig 一起寫入 connection.kubeconfig。
#    正式環境此步由 PaaS 平台 seed；本機示範可用 docker exec 就地寫 /data/config.json 後 docker restart。

# 5. 瀏覽器登入 admin（http://localhost:8080）→ Connection 頁確認 kubeconfig 已載入
#    → Test 應為綠（子行程 running 時走 tools/call namespaces_list 真驗）

# 6. 直打 MCP 端點（token 取自 Tokens 頁 / rotate）——全開時 tools/list 為 34 支（多 context 的 kubeconfig 為 35 支）
curl -s -X POST http://localhost:8080/private_<token>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"namespaces_list","arguments":{}}}'
```

## 部署後 mode-aware 對帳 smoke

對 `/private_<token>/mcp` 取 `tools/list`，與 `tool_registry.expected_exposed_tools()` 依當前
治理模式推導的預期子集比對（`read_only` / `disable_destructive` / `disabled` / `toolsets` 取自實際設定）：

```python
# 於 apps/k8s 目錄下（uv run --directory apps/k8s python …）
from k8s_mcp_admin.tool_registry import expected_exposed_tools
# 全開（六個工具集）→ 34 支；kubeconfig 有多個 context（多叢集預設開）→ multiple_contexts=True，35 支
print(sorted(expected_exposed_tools(read_only=False, disable_destructive=False)))
# 唯讀 → 19 支；只開 disable_destructive → 34 - 9 = 25 支
# 2026-10-25 之前的全鎖 seed（READ_ONLY=1、DISABLE_DESTRUCTIVE=1、core,config,helm）→ 唯讀 15 支
print(sorted(expected_exposed_tools(read_only=True, disable_destructive=True, toolsets=["core", "config", "helm"])))
```

把 `tools/list` 回傳的名單與上式輸出比對，一致即綠。

## CI smoke 侷限

CI（`.gitea/workflows/ci.yml`）的 smoke 只驗 admin `/healthz`：**無 config、子行程不啟動**屬預期，
**不覆蓋** binary 對叢集的實際能力。端到端能力靠上述「本機 e2e 劇本」與「部署後 mode-aware 對帳」。
CI 另跑 `uv run --directory apps/k8s pytest -q`（launcher / config router / cluster_probe /
mcp_client / health / tool_registry 全套保護網）。

## 升版 SOP（禁 latest tag、禁 PyPI/npm 路線）

1. 取新版 binary 的 sha256：
   ```bash
   gh api repos/containers/kubernetes-mcp-server/releases/tags/v<新版> \
     --jq '.assets[]|select(.name=="kubernetes-mcp-server-linux-amd64").digest'
   ```
2. `apps/k8s/Dockerfile` 的 **`KMS_VERSION`** 與 `ADD --checksum=sha256:…` **成對更新**
   （hash 不符 build 即 fail）。
3. 對新版逐模式（全量 / `--read-only` / `--disable-destructive`）取 `tools/list`，**重抄**
   `tool_registry.py` 的 35 支名單（六個工具集＋多叢集的 configuration_contexts_list；重新確認哪些工具集不需另外設定）、`readOnlyHint` /
   `destructiveHint` annotations 與各數字
   （尤其複查 `pods_run` / `helm_install` 這類 dangerous 但非 destructive 的例外是否隨上游調整）。
4. 跑契約測試（`uv run --directory packages/mcp-admin-core pytest -q` +
   `uv run --directory apps/k8s pytest -q`）→ 全綠後走 PR。

> ⛔ **禁止**改用 PyPI wheel `kubernetes-mcp-server`：那是 20KB 純 Python **runtime 下載器**
> （首跑抓 GitHub、無 checksum、cache 吃 `$HOME`），與本 app「build-time 釘 hash、runtime 零下載」
> 的安全模型衝突。npm 路線同禁。

## Claude Code client 設定範例

```json
{
  "mcpServers": {
    "my-k8s-mcp": {
      "type": "http",
      "url": "https://<host>/private_<token>/mcp"
    }
  }
}
```

`<host>` = 部署後對外主機、`<token>` 取自 admin 的 **Tokens** 頁。transport 用 Streamable HTTP
（`/mcp`），勿用 `/sse`。

## 相關文件

- 架構分層與資料流：[`docs/architecture.md`](docs/architecture.md)
- 實作計畫（完整決策脈絡）：`docs/superpowers/plans/k8s-mcp-app.md`（repo root 相對）
