"""k8s MCP server 的 HTTP launcher（打包上游 containers/kubernetes-mcp-server v0.0.65）。

上游 Go binary ``kubernetes-mcp-server`` 設了 ``--port`` 才起 HTTP（同埠同時掛
``POST/GET /mcp`` Streamable HTTP、``GET /sse``、``GET /healthz`` 等）；不設 = stdio。
本 console script（``k8s-mcp-http``）把 admin 的 config.json / 治理 env 翻成上游啟動
參數，最後以 ``execvpe`` 取代本行程——讓 mcp-admin 的 ``McpProcessManager`` 直接管到
Go 子行程（restart 的 SIGTERM 可達），reverse proxy（``/private_{token}/mcp`` →
``127.0.0.1:{port}/mcp``）把 client 轉進來。

三段責任：

1. **kubeconfig 派生檔**：``K8S_MCP_KUBECONFIG``（內容）→ ``{data}/kubeconfig``
   （0600、原子寫）；``in_cluster`` 或空值時主動刪檔（Go binary 改走 in-cluster
   SA 自動偵測）。exec 前 ``env.pop`` 已消化的 ``K8S_MCP_*`` 七鍵，防 kubeconfig
   全文躺在 Go 行程的 ``/proc/<pid>/environ``。
2. **治理 TOML**：產出單一 ``{data}/k8s-mcp.toml``——三顆治理 toggle（read_only /
   disable_destructive / ALLOW_SECRETS↔denied_resources）＋ toolsets ＋ per-tool
   ``disabled_tools``（自 config.json ``tools.disabled`` 讀，Tools 頁 toggle →
   restart 即真傳導）＋ ``confirmation_fallback="allow"``。
3. **argv 組裝**：``kubernetes-mcp-server --port {port} --bind-address 127.0.0.1
   [--disable-multi-cluster] --config {toml} [--kubeconfig {path}]``（``K8S_MCP_MULTI_CLUSTER`` 開時不帶
   ``--disable-multi-cluster``）。

**單一通道規則**（違反即錯）：每個設定鍵只走 flag 或 TOML 其一，嚴禁重複——
port / bind-address / disable-multi-cluster / kubeconfig 走 **flag**；
read_only / disable_destructive / toolsets / disabled_tools / denied_resources /
confirmation_fallback 走 **TOML**。

**安全邊界**：``--bind-address 127.0.0.1`` 硬編（上游預設 0.0.0.0；proxy 的 path-token
是唯一認證層，子行程只能綁 loopback），不受 env / seed 影響。

**多叢集**（負責人 2026-10-25 決定預設開）：``K8S_MCP_MULTI_CLUSTER`` 開＝不帶 ``--disable-multi-cluster``——
kubeconfig 有多個 context 時，上游多一支 ``configuration_contexts_list``、其他工具多一個 ``context`` 參數，
AI 可以指定 context 操作；只有一個 context 時與單叢集相同。關（或缺鍵）＝帶 ``--disable-multi-cluster``，
一律用 current-context。

新安裝的 config.json（負責人 2026-10-25「預設全開」；PaaS 範本 seed 明寫同一組值，本行程自己建立
config.json 時由 ``LaunchPolicy.fresh_env``＝``FRESH_ENV`` 寫入）::

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
    }

toolsets＝上游 v0.0.65 不需另外設定就能用的全部工具集（34 支）；``kiali`` 必須設定 Kiali 網址
（``[toolset_configs.kiali] url``），``netobserv`` 的預設網址是「MCP 所在叢集」的 Service，連外部叢集
也要設定網址，所以兩者不開。

既有 config.json 第一次以本映像啟動時，由 core 的 ``owner_defaults`` 一次性改成這組值（並寫上標記；之後管理員的
限制會保留）；toolsets 例外，取舊值與 ``FRESH_TOOLSETS`` 的聯集（舊值已涵蓋就不動；否則 ``FRESH_TOOLSETS`` 的順序在前、
再補上舊值多的項目，去重），管理員原本加的 kiali、netobserv 等不會被拿掉（``LaunchPolicy.fresh_env_union_keys``）；
改版前的出廠值 ``core,config,helm`` 因此正好變成上面的全開值。已有標記卻缺鍵時（例如手動刪掉）launcher 維持改版前的行為：唯讀與擋破壞性為關、**擋 Secret**、
toolsets ``core,config,helm``、單叢集。

connection 的 ``kubeconfig`` / ``in_cluster`` 經 ``env_prefix="K8S_MCP_"`` 注入為
``K8S_MCP_KUBECONFIG`` / ``K8S_MCP_IN_CLUSTER``；三顆治理 toggle 走 ``mcp_server.env``
（原樣、不加前綴），故 seed 直接寫全名。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from k8s_mcp_admin.cluster_probe import IN_CLUSTER_UNAVAILABLE_MESSAGE, in_cluster_available

# 部署時 admin 消化後即 pop 的 K8S_MCP_* 七鍵——避免 kubeconfig 全文與治理旗標
# 隨 execvpe 落進 Go 子行程的環境（/proc/<pid>/environ 可讀）。
_CONSUMED_ENV_KEYS = (
    "K8S_MCP_KUBECONFIG",
    "K8S_MCP_IN_CLUSTER",
    "K8S_MCP_READ_ONLY",
    "K8S_MCP_DISABLE_DESTRUCTIVE",
    "K8S_MCP_ALLOW_SECRETS",
    "K8S_MCP_TOOLSETS",
    "K8S_MCP_MULTI_CLUSTER",
)

# 新安裝的 toolsets：上游 v0.0.65 不需另外設定就能用的全部工具集（34 支；kiali 要 Kiali 網址、netobserv 的
# 預設網址只對 MCP 所在叢集有效，兩者不開）。工具清單見 tool_registry。
FRESH_TOOLSETS = ["config", "core", "helm", "kcp", "kubevirt", "tekton"]

# 全開的治理開關（LaunchPolicy.fresh_env；PaaS 範本 seed 同值；既有設定由 owner_defaults 一次性改成這組）：
# 不唯讀、不擋破壞性、可讀 Secret、免設定的全部工具集、多叢集。
FRESH_ENV = {
    "K8S_MCP_READ_ONLY": "0",
    "K8S_MCP_DISABLE_DESTRUCTIVE": "0",
    "K8S_MCP_ALLOW_SECRETS": "1",
    "K8S_MCP_TOOLSETS": ",".join(FRESH_TOOLSETS),
    "K8S_MCP_MULTI_CLUSTER": "1",
}

# K8S_MCP_TOOLSETS 缺鍵時（既有設定、手動建立）沿用改版前的 core,config,helm（23 支；上游 BaseDefault
# 不含 helm，故必須顯式含 helm，見 plan 附錄 E）。ALLOW_SECRETS 缺鍵時也維持擋 Secret（見 main()）。
_DEFAULT_TOOLSETS = ["core", "config", "helm"]

# ALLOW_SECRETS 關閉時預設封鎖的 GVK（擋 Secret v1；array-of-table 形對齊上游
# api.GroupVersionKind 的 toml 標籤 group/version/kind）。
_DENIED_SECRET = {"group": "", "version": "v1", "kind": "Secret"}


def _toggle_on(value: str | None) -> bool:
    """治理旗標判定（對齊 SettingsPage 與 config router 的正規化）。"""
    return (value or "").strip().lower() in ("1", "true", "yes")


def _config_path() -> Path:
    """config.json 完整路徑（對齊 ConfigStore：直接用 MCP_ADMIN_CONFIG 全值，
    不重新硬編 basename——若 env 指向非預設檔名，disabled_tools 仍讀對檔）。"""
    return Path(os.environ.get("MCP_ADMIN_CONFIG", "/data/config.json"))


def _data_dir() -> Path:
    """資料目錄 = config.json 所在目錄（預設 /data；kubeconfig / toml 派生檔基準）。"""
    return _config_path().parent


def _atomic_write_0600(path: Path, content: str) -> None:
    """同目錄 tmp 檔（0600）寫入 → ``os.replace`` 原子換名。

    首建瞬間即 0600（避免 ``write_text`` 後補 chmod 的 umask 0644 落盤窗口），
    且順手獲得原子寫——restart 重寫派生檔時不會被子行程讀到半截內容。
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o600)  # mkstemp 本就 0600，明確化以防平台差異
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _read_disabled_tools(config_path: Path) -> list[str]:
    """自 config.json 讀 ``tools.disabled``（per-tool toggle 真傳導來源）。

    容錯：檔不存在 / 壞 JSON / 結構不符 → 回空 list（launcher 不得因設定檔缺漏
    而 fail-fast——CI smoke 無 config 亦需能起）。
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    tools = data.get("tools")
    if not isinstance(tools, dict):
        return []
    disabled = tools.get("disabled")
    if not isinstance(disabled, list):
        return []
    return [str(t) for t in disabled]


def _render_toml(
    read_only: bool,
    disable_destructive: bool,
    toolsets: list[str],
    disabled_tools: list[str],
    allow_secrets: bool,
) -> str:
    """手寫上游 ``StaticConfig`` TOML（字串值用 ``json.dumps`` 轉義——JSON 字串是
    合法 TOML 字串；bool 的 ``json.dumps`` 即 true/false，恰為 TOML bool 字面）。"""

    def _arr(items: list[str]) -> str:
        return "[" + ", ".join(json.dumps(i) for i in items) + "]"

    lines = [
        f"read_only = {json.dumps(read_only)}",
        f"disable_destructive = {json.dumps(disable_destructive)}",
        f"toolsets = {_arr(toolsets)}",
    ]
    if disabled_tools:
        lines.append(f"disabled_tools = {_arr(disabled_tools)}")
    lines.append('confirmation_fallback = "allow"')
    if not allow_secrets:
        # array-of-table 須置於頂層鍵之後
        lines += [
            "",
            "[[denied_resources]]",
            f'group = {json.dumps(_DENIED_SECRET["group"])}',
            f'version = {json.dumps(_DENIED_SECRET["version"])}',
            f'kind = {json.dumps(_DENIED_SECRET["kind"])}',
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    """Console script 進入點（``k8s-mcp-http``）。"""
    parser = argparse.ArgumentParser(
        description=(
            "Run the upstream kubernetes-mcp-server over Streamable HTTP "
            "(fronted by the mcp-admin proxy)."
        )
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="bind port；必須等於 config mcp_server.port（proxy 反代目標，預設 8000）",
    )
    args = parser.parse_args(argv)

    data_dir = _data_dir()
    config_path = _config_path()
    env = dict(os.environ)

    # --- (1) kubeconfig 派生檔與 in-cluster 分支 ---
    kubeconfig_content = env.get("K8S_MCP_KUBECONFIG", "")
    in_cluster = _toggle_on(env.get("K8S_MCP_IN_CLUSTER"))
    kubeconfig_path = data_dir / "kubeconfig"
    if in_cluster and not in_cluster_available():
        # #1352：沒掛 SA token 時 Go binary 的 in-cluster 偵測必然失敗、只留難懂的錯誤。
        # 在這裡印可讀原因（admin 會收進 log／Settings 的 MCP 狀態）後以 exit 1 結束。
        print(f"k8s-mcp-http: {IN_CLUSTER_UNAVAILABLE_MESSAGE}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    use_kubeconfig = False
    if not in_cluster and kubeconfig_content.strip():
        _atomic_write_0600(kubeconfig_path, kubeconfig_content)
        use_kubeconfig = True
    else:
        # in-cluster 或空值：撤下憑證派生檔（切回需重貼；in-cluster 走 SA 自動偵測）
        try:
            kubeconfig_path.unlink()
        except FileNotFoundError:
            pass
        if not in_cluster and not in_cluster_available():
            # 未設 kubeconfig、也沒有 SA token：上游只能 auto-detect，多半起不來。先印可讀提示
            # （不改變行為，仍交給上游判定）。
            print(
                "k8s-mcp-http: 尚未設定 kubeconfig——請到管理頁 Connection 貼上你自己叢集的 kubeconfig。",
                file=sys.stderr,
                flush=True,
            )

    # --- (2) 治理 TOML ---
    # 缺鍵＝改版前的行為（不唯讀、不擋破壞性、擋 Secret、core,config,helm、單叢集）；全開的值見 FRESH_ENV。
    read_only = _toggle_on(env.get("K8S_MCP_READ_ONLY"))
    disable_destructive = _toggle_on(env.get("K8S_MCP_DISABLE_DESTRUCTIVE"))
    allow_secrets = _toggle_on(env.get("K8S_MCP_ALLOW_SECRETS"))
    toolsets_raw = env.get("K8S_MCP_TOOLSETS", "")
    if toolsets_raw.strip():
        toolsets = [t.strip() for t in toolsets_raw.split(",") if t.strip()]
    else:
        toolsets = list(_DEFAULT_TOOLSETS)
    disabled_tools = _read_disabled_tools(config_path)
    multi_cluster = _toggle_on(env.get("K8S_MCP_MULTI_CLUSTER"))

    toml_path = data_dir / "k8s-mcp.toml"
    _atomic_write_0600(
        toml_path,
        _render_toml(read_only, disable_destructive, toolsets, disabled_tools, allow_secrets),
    )

    # --- exec 前清掉已消化的 K8S_MCP_* env ---
    for key in _CONSUMED_ENV_KEYS:
        env.pop(key, None)

    # --- (3) argv 組裝與 execvpe ---
    argv_out = [
        "kubernetes-mcp-server",
        "--port",
        str(args.port),
        "--bind-address",
        "127.0.0.1",  # 安全邊界硬編：只綁 loopback，認證交給 proxy path-token
    ]
    if not multi_cluster:
        argv_out.append("--disable-multi-cluster")  # 單叢集：一律用 current-context
    argv_out += ["--config", str(toml_path)]
    if use_kubeconfig:
        argv_out += ["--kubeconfig", str(kubeconfig_path)]

    os.execvpe("kubernetes-mcp-server", argv_out, env)


if __name__ == "__main__":
    main()
