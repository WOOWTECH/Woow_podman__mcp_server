"""PaaS 未掛 ServiceAccount token 時 in-cluster 的可讀原因（#1352）。

PaaS 部署 automountServiceAccountToken=false → /var/run/secrets/.../token 不存在 →
in-cluster 必然失敗。驗證：PUT 擋下（不清掉既有 kubeconfig）、Test／health 回可讀原因、
launcher 印原因後 exit 1（不 exec Go binary）。
"""
from __future__ import annotations

from management_client import revision_headers

import json

import pytest

_MIN_KUBECONFIG = json.dumps(
    {
        "apiVersion": "v1",
        "clusters": [{"name": "c1", "cluster": {"server": "https://10.0.0.1:6443"}}],
        "contexts": [{"name": "ctx1", "context": {"cluster": "c1", "user": "u"}}],
        "current-context": "ctx1",
        "users": [{"name": "u", "user": {}}],
    }
)


@pytest.fixture
def no_sa_token(monkeypatch, tmp_path):
    from k8s_mcp_admin import cluster_probe

    monkeypatch.setattr(cluster_probe, "IN_CLUSTER_TOKEN_PATH", str(tmp_path / "missing-token"))


def test_in_cluster_available_follows_token_file(no_sa_token):
    from k8s_mcp_admin import cluster_probe

    assert cluster_probe.in_cluster_available() is False
    assert cluster_probe.wants_in_cluster({"in_cluster": "yes"}) is True
    assert cluster_probe.wants_in_cluster({"in_cluster": "false"}) is False


def test_in_cluster_available_when_mounted():
    # conftest 的 autouse fixture 已模擬掛載
    from k8s_mcp_admin import cluster_probe

    assert cluster_probe.in_cluster_available() is True


def test_put_in_cluster_true_rejected_and_kubeconfig_kept(no_sa_token, make_client, config_path):
    client = make_client(connection={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    resp = client.put(
        "/api/config/connection", json={"kubeconfig": "", "in_cluster": "true", "restart": False}
    , headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 400
    assert "未掛載叢集權杖" in resp.json()["detail"]
    assert "kubeconfig" in resp.json()["detail"]
    conn = json.loads(config_path.read_text())["connection"]
    assert conn["kubeconfig"] == _MIN_KUBECONFIG  # 沒被清空
    assert conn["in_cluster"] == "false"


def test_put_in_cluster_false_still_ok(no_sa_token, make_client):
    client = make_client(connection={"in_cluster": "false"})
    resp = client.put(
        "/api/config/connection",
        json={"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false", "restart": False},
     headers=revision_headers(client, None, read_path='/api/config'))
    assert resp.status_code == 200


def test_test_endpoint_reports_missing_token(no_sa_token, make_client):
    # 既有存檔（例如舊版存下的 in_cluster=true）→ Test 直接回可讀原因
    client = make_client(connection={"kubeconfig": "", "in_cluster": "true"})
    body = client.post("/api/config/test").json()
    assert body["success"] is False
    assert body["details"]["auth"] == "in_cluster_unavailable"
    assert "請把 In-Cluster 設為 false" in body["message"]


async def test_health_probe_reports_missing_token(no_sa_token):
    from k8s_mcp_admin import health_probes

    out = await health_probes.probe_target({"kubeconfig": "", "in_cluster": "true"})
    assert out["healthy"] is False
    assert "未掛載叢集權杖" in out["error"]


def test_launcher_exits_with_readable_reason(no_sa_token, monkeypatch, tmp_path, capsys):
    from k8s_mcp_admin import launcher

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("K8S_MCP_IN_CLUSTER", "true")
    monkeypatch.delenv("K8S_MCP_KUBECONFIG", raising=False)

    def _no_exec(*a, **k):  # pragma: no cover - 走到這裡就是 bug
        raise AssertionError("should not exec kubernetes-mcp-server")

    monkeypatch.setattr(launcher.os, "execvpe", _no_exec)
    with pytest.raises(SystemExit) as exc:
        launcher.main(["--port", "8000"])
    assert exc.value.code == 1
    assert "未掛載叢集權杖" in capsys.readouterr().err


def test_launcher_hints_when_no_kubeconfig(no_sa_token, monkeypatch, tmp_path, capsys):
    from k8s_mcp_admin import launcher

    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("K8S_MCP_IN_CLUSTER", "false")
    monkeypatch.delenv("K8S_MCP_KUBECONFIG", raising=False)
    called = {}
    monkeypatch.setattr(launcher.os, "execvpe", lambda f, argv, env: called.setdefault("argv", argv))
    launcher.main(["--port", "8000"])
    assert "argv" in called  # 行為不變，仍交給上游
    assert "尚未設定 kubeconfig" in capsys.readouterr().err
