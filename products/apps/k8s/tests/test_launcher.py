"""k8s MCP HTTP launcher 的契約測試（錨定上游 v0.0.65）。

launcher（console script ``k8s-mcp-http``）以三段責任把 config.json / 治理 env
翻成上游 Go binary ``kubernetes-mcp-server`` 的啟動參數：

1. kubeconfig 派生檔（``{data}/kubeconfig``、0600、原子寫；in-cluster 或空值刪檔）
   ＋ exec 前 pop 已消化的 ``K8S_MCP_*`` env，防 kubeconfig 全文躺在 Go 行程的
   ``/proc/<pid>/environ``。
2. 產出單一 ``{data}/k8s-mcp.toml``（治理三 toggle ＋ per-tool ``disabled_tools``
   真傳導 ＋ ``confirmation_fallback="allow"`` ＋ 預設擋 Secret 的 ``denied_resources``）。
3. argv 組裝與 ``execvpe``：``kubernetes-mcp-server --port … --bind-address 127.0.0.1
   --disable-multi-cluster --config … [--kubeconfig …]``。

**單一通道規則**：port / bind-address / disable-multi-cluster / kubeconfig 走 flag；
read_only / disable_destructive / toolsets / disabled_tools / denied_resources /
confirmation_fallback 走 TOML，兩邊嚴禁重複。
"""
from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把 MCP_ADMIN_CONFIG 指到 tmp 目錄的 config.json，回傳資料目錄。"""
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_path / "config.json"))
    return tmp_path


@pytest.fixture
def clean_k8s_env(monkeypatch):
    """清掉所有 K8S_MCP_* env，避免測試機殘留污染。"""
    for k in list(os.environ):
        if k.startswith("K8S_MCP_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def run_launcher(monkeypatch, clean_k8s_env):
    """呼叫 launcher.main 並攔截 os.execvpe，回傳 (argv, env)。"""
    from k8s_mcp_admin import launcher

    captured: dict = {}

    def fake_execvpe(file, argv, env):
        captured["file"] = file
        captured["argv"] = argv
        captured["env"] = env
        # exec 不返回；以 SystemExit 模擬替換行程
        raise SystemExit(0)

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    def _run(argv=None):
        # 預設傳空 list（而非 None）——避免 argparse 落到 pytest 自己的 sys.argv。
        with pytest.raises(SystemExit):
            launcher.main([] if argv is None else argv)
        return captured

    return _run


def _write_config_json(data_dir: Path, payload: dict) -> None:
    (data_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# Task 8：kubeconfig 派生檔與 env 清理
# --------------------------------------------------------------------------


def test_kubeconfig_written_0600_atomic(monkeypatch, data_dir, run_launcher):
    content = "apiVersion: v1\nclusters:\n- name: c1\ncurrent-context: c1\n"
    monkeypatch.setenv("K8S_MCP_KUBECONFIG", content)

    run_launcher()

    path = data_dir / "kubeconfig"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == content
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_in_cluster_deletes_stale_kubeconfig(monkeypatch, data_dir, run_launcher):
    stale = data_dir / "kubeconfig"
    stale.write_text("apiVersion: v1\n", encoding="utf-8")
    monkeypatch.setenv("K8S_MCP_IN_CLUSTER", "true")

    captured = run_launcher()

    assert not stale.exists()
    assert "--kubeconfig" not in captured["argv"]


def test_empty_kubeconfig_no_flag(monkeypatch, data_dir, run_launcher):
    # 完全沒有 K8S_MCP_KUBECONFIG（clean_k8s_env 已清）
    captured = run_launcher()

    assert not (data_dir / "kubeconfig").exists()
    assert "--kubeconfig" not in captured["argv"]


def test_env_pop_before_exec(monkeypatch, data_dir, run_launcher):
    monkeypatch.setenv("K8S_MCP_KUBECONFIG", "apiVersion: v1\n")
    monkeypatch.setenv("K8S_MCP_IN_CLUSTER", "false")
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "1")
    monkeypatch.setenv("K8S_MCP_DISABLE_DESTRUCTIVE", "1")
    monkeypatch.setenv("K8S_MCP_ALLOW_SECRETS", "0")
    monkeypatch.setenv("K8S_MCP_TOOLSETS", "core,config,helm")
    monkeypatch.setenv("K8S_MCP_MULTI_CLUSTER", "1")

    captured = run_launcher()

    env = captured["env"]
    for k in (
        "K8S_MCP_KUBECONFIG",
        "K8S_MCP_IN_CLUSTER",
        "K8S_MCP_READ_ONLY",
        "K8S_MCP_DISABLE_DESTRUCTIVE",
        "K8S_MCP_ALLOW_SECRETS",
        "K8S_MCP_TOOLSETS",
        "K8S_MCP_MULTI_CLUSTER",
    ):
        assert k not in env


# --------------------------------------------------------------------------
# Task 9：/data/k8s-mcp.toml（治理 + per-tool 真傳導）
# --------------------------------------------------------------------------


def _load_toml(data_dir: Path) -> dict:
    return tomllib.loads((data_dir / "k8s-mcp.toml").read_text(encoding="utf-8"))


def test_toml_fresh_install_is_all_on(monkeypatch, data_dir, run_launcher):
    """新安裝的 env（launcher.FRESH_ENV＝PaaS 範本 seed）：不唯讀、不擋破壞性、不擋 Secret、六個免設定工具集。"""
    from k8s_mcp_admin.launcher import FRESH_ENV

    assert FRESH_ENV == {
        "K8S_MCP_READ_ONLY": "0",
        "K8S_MCP_DISABLE_DESTRUCTIVE": "0",
        "K8S_MCP_ALLOW_SECRETS": "1",
        "K8S_MCP_TOOLSETS": "config,core,helm,kcp,kubevirt,tekton",
        "K8S_MCP_MULTI_CLUSTER": "1",
    }
    for key, value in FRESH_ENV.items():
        monkeypatch.setenv(key, value)

    captured = run_launcher()

    assert "--disable-multi-cluster" not in captured["argv"], "全開＝多叢集"
    cfg = _load_toml(data_dir)
    assert cfg["read_only"] is False
    assert cfg["disable_destructive"] is False
    assert cfg["toolsets"] == ["config", "core", "helm", "kcp", "kubevirt", "tekton"]
    assert "denied_resources" not in cfg
    assert cfg["confirmation_fallback"] == "allow"


def test_toml_governance_defaults(data_dir, run_launcher):
    """缺鍵（既有設定、手動建立）＝改版前的行為：擋 Secret、core,config,helm；不會因換映像打開。"""
    run_launcher()

    cfg = _load_toml(data_dir)
    assert cfg["read_only"] is False
    assert cfg["disable_destructive"] is False
    assert cfg["toolsets"] == ["core", "config", "helm"]
    assert cfg["confirmation_fallback"] == "allow"
    assert cfg["denied_resources"] == [
        {"group": "", "version": "v1", "kind": "Secret"}
    ]
    assert cfg.get("disabled_tools", []) == []


def test_toml_toggles_on(monkeypatch, data_dir, run_launcher):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "1")
    monkeypatch.setenv("K8S_MCP_DISABLE_DESTRUCTIVE", "true")
    monkeypatch.setenv("K8S_MCP_ALLOW_SECRETS", "yes")

    run_launcher()

    cfg = _load_toml(data_dir)
    assert cfg["read_only"] is True
    assert cfg["disable_destructive"] is True
    # ALLOW_SECRETS 開 = 移除封鎖
    assert "denied_resources" not in cfg


def test_toml_toolsets_from_env(monkeypatch, data_dir, run_launcher):
    monkeypatch.setenv("K8S_MCP_TOOLSETS", "core,config")

    run_launcher()

    cfg = _load_toml(data_dir)
    assert cfg["toolsets"] == ["core", "config"]


def test_toml_disabled_tools_from_config_json(data_dir, run_launcher):
    _write_config_json(data_dir, {"tools": {"disabled": ["helm_install", "pods_exec"]}})

    run_launcher()

    cfg = _load_toml(data_dir)
    assert set(cfg["disabled_tools"]) == {"helm_install", "pods_exec"}


def test_toml_disabled_tools_from_nondefault_config_path(tmp_path, monkeypatch, run_launcher):
    # MCP_ADMIN_CONFIG 指向非 config.json 的檔名時，disabled_tools 仍須自該檔讀取
    # （ConfigStore 用完整 MCP_ADMIN_CONFIG，launcher 不可硬編 config.json basename）。
    config_path = tmp_path / "custom.json"
    config_path.write_text(
        json.dumps({"tools": {"disabled": ["helm_install"]}}), encoding="utf-8"
    )
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config_path))

    run_launcher()

    cfg = _load_toml(tmp_path)
    assert set(cfg["disabled_tools"]) == {"helm_install"}


def test_toml_disabled_tools_bad_json_tolerated(data_dir, run_launcher):
    (data_dir / "config.json").write_text("{not valid json", encoding="utf-8")

    run_launcher()

    cfg = _load_toml(data_dir)
    assert cfg.get("disabled_tools", []) == []


def test_toml_file_mode_0600(data_dir, run_launcher):
    run_launcher()

    path = data_dir / "k8s-mcp.toml"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --------------------------------------------------------------------------
# Task 10：argv 組裝與 execvpe
# --------------------------------------------------------------------------


def test_argv_shape_with_kubeconfig(monkeypatch, data_dir, run_launcher):
    monkeypatch.setenv("K8S_MCP_KUBECONFIG", "apiVersion: v1\n")

    captured = run_launcher()

    assert captured["file"] == "kubernetes-mcp-server"
    argv = captured["argv"]
    assert argv[0] == "kubernetes-mcp-server"
    assert _flag_value(argv, "--port") == "8000"
    assert _flag_value(argv, "--bind-address") == "127.0.0.1"
    assert "--disable-multi-cluster" in argv
    assert _flag_value(argv, "--config") == str(data_dir / "k8s-mcp.toml")
    assert _flag_value(argv, "--kubeconfig") == str(data_dir / "kubeconfig")


def test_argv_no_kubeconfig_in_cluster(monkeypatch, data_dir, run_launcher):
    monkeypatch.setenv("K8S_MCP_IN_CLUSTER", "true")

    captured = run_launcher()

    assert "--kubeconfig" not in captured["argv"]
    assert "--disable-multi-cluster" in captured["argv"]


@pytest.mark.parametrize("value, disabled", [(None, True), ("0", True), ("1", False), ("true", False)])
def test_multi_cluster_toggle(monkeypatch, data_dir, run_launcher, value, disabled):
    """K8S_MCP_MULTI_CLUSTER 開＝不帶 --disable-multi-cluster；關或缺鍵＝單叢集（改版前的行為）。"""
    monkeypatch.setenv("K8S_MCP_KUBECONFIG", "apiVersion: v1\n")
    if value is not None:
        monkeypatch.setenv("K8S_MCP_MULTI_CLUSTER", value)

    captured = run_launcher()

    assert ("--disable-multi-cluster" in captured["argv"]) is disabled
    assert _flag_value(captured["argv"], "--bind-address") == "127.0.0.1"
    assert _flag_value(captured["argv"], "--config") == str(data_dir / "k8s-mcp.toml")


def test_argv_custom_port(data_dir, run_launcher):
    captured = run_launcher(["--port", "9000"])

    assert _flag_value(captured["argv"], "--port") == "9000"


def test_bind_address_always_loopback(monkeypatch, data_dir, run_launcher):
    # 任何輸入組合 argv 都含 --bind-address 127.0.0.1（安全邊界硬編）
    monkeypatch.setenv("K8S_MCP_KUBECONFIG", "apiVersion: v1\n")
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "0")

    captured = run_launcher(["--port", "12345"])

    assert _flag_value(captured["argv"], "--bind-address") == "127.0.0.1"


def _flag_value(argv: list[str], flag: str) -> str:
    idx = argv.index(flag)
    return argv[idx + 1]
