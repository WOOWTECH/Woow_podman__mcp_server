"""health_probes（probe_target / fetch_info，供 make_health_router 使用）的契約測試。

dashboard 語意化：
- target_app = 叢集可達性（匿名 GET /version）。
- db_name = current-context 名（in-cluster 顯示 "in-cluster"）。
- version = gitVersion；item_count 一律 None（k8s 無單一計數語意）。

**startup/health 不得因缺設定炸掉**（CI smoke 前提）：connection 全空時 probe_target /
fetch_info 皆須 graceful 回 degraded 而非 raise。
"""
from __future__ import annotations

import yaml

# 合法最小 kubeconfig（current-context = ctx1）。
_MIN_KUBECONFIG = yaml.safe_dump(
    {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c1", "cluster": {"server": "https://api:6443"}}],
        "contexts": [{"name": "ctx1", "context": {"cluster": "c1", "user": "u"}}],
        "current-context": "ctx1",
        "users": [{"name": "u", "user": {}}],
    }
)


def test_probe_target_healthy(monkeypatch):
    from k8s_mcp_admin import cluster_probe, health_probes

    async def _fake_fetch(target, *, transport=None):
        return True, "v1.31.0", "叢集可達（版本 v1.31.0）"

    monkeypatch.setattr(cluster_probe, "fetch_version", _fake_fetch)

    import asyncio

    result = asyncio.run(
        health_probes.probe_target({"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    )
    assert result["healthy"] is True


def test_probe_target_unreachable(monkeypatch):
    from k8s_mcp_admin import cluster_probe, health_probes

    async def _fake_fetch(target, *, transport=None):
        return False, None, "無法連上叢集 API server：Connection refused"

    monkeypatch.setattr(cluster_probe, "fetch_version", _fake_fetch)

    import asyncio

    result = asyncio.run(
        health_probes.probe_target({"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    )
    assert result["healthy"] is False
    assert result.get("message") or result.get("error")


def test_fetch_info_kubeconfig_mode(monkeypatch):
    from k8s_mcp_admin import cluster_probe, health_probes

    async def _fake_fetch(target, *, transport=None):
        return True, "v1.31.0", "叢集可達（版本 v1.31.0）"

    monkeypatch.setattr(cluster_probe, "fetch_version", _fake_fetch)

    import asyncio

    info = asyncio.run(
        health_probes.fetch_info({"kubeconfig": _MIN_KUBECONFIG, "in_cluster": "false"})
    )
    assert info["version"] == "v1.31.0"
    assert info["db_name"] == "ctx1"
    assert info["item_count"] is None


def test_fetch_info_in_cluster(monkeypatch):
    from k8s_mcp_admin import cluster_probe, health_probes

    async def _fake_fetch(target, *, transport=None):
        return True, "v1.30.0", "叢集可達（版本 v1.30.0）"

    monkeypatch.setattr(cluster_probe, "fetch_version", _fake_fetch)

    import asyncio

    info = asyncio.run(health_probes.fetch_info({"kubeconfig": "", "in_cluster": "true"}))
    assert info["db_name"] == "in-cluster"


def test_probe_no_config_graceful(monkeypatch):
    from k8s_mcp_admin import health_probes

    import asyncio

    # connection 全空 → 不 raise、healthy False + 說明。
    result = asyncio.run(health_probes.probe_target({"kubeconfig": "", "in_cluster": "false"}))
    assert result["healthy"] is False
    assert result.get("error") or result.get("message")

    # fetch_info 同樣 graceful（不 raise）。
    info = asyncio.run(health_probes.fetch_info({"kubeconfig": "", "in_cluster": "false"}))
    assert info["version"] is None
    assert info["item_count"] is None
