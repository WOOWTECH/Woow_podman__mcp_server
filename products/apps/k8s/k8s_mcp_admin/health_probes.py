"""k8s app-specific health 探測（供 mcp_admin_core make_health_router 使用）。

函式簽名比照 apps/odoo-manage/odoo_manage_mcp_admin/health_probes.py：
- ``probe_target(conn)`` → ``{healthy, url, ...}``（dashboard target_app 卡片）。
- ``fetch_info(conn)``  → ``{version, db_name, item_count}``（版本／context 名／計數）。

dashboard 語意化（決策 4）：
- target_app = 叢集可達性（cluster_probe 匿名 GET /version）。
- db_name = current-context 名（in-cluster 顯示 "in-cluster"）。
- version = gitVersion；item_count 一律 None（k8s 無單一計數語意）。

**startup/health 不得因缺設定炸掉**（CI smoke 前提）：全程 try/except，缺設定或探測失敗
一律回 degraded（healthy False / version None），絕不 raise。cluster_probe 內建 timeout
（總 10s、connect 5s）故不會卡死 dashboard。
"""
from __future__ import annotations

import logging
from typing import Any

from k8s_mcp_admin import cluster_probe

logger = logging.getLogger(__name__)


async def probe_target(conn: dict) -> dict[str, Any]:
    """探叢集 API server 可達性（匿名 GET /version），回 make_health_router 需的形狀。"""
    if cluster_probe.wants_in_cluster(conn) and not cluster_probe.in_cluster_available():
        # #1352：in-cluster 但沒掛 SA token（PaaS 預設）→ 子行程必然起不來，直接給可讀原因。
        return {"healthy": False, "url": "", "error": cluster_probe.IN_CLUSTER_UNAVAILABLE_MESSAGE}
    try:
        target = cluster_probe.resolve_target(conn)
    except ValueError as exc:
        # 缺 kubeconfig 且非 in-cluster：無探測目標，回 degraded 不 raise。
        return {"healthy": False, "url": "", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 任何解析失敗都不得炸掉 dashboard
        logger.warning("k8s probe_target 解析設定失敗：%s", exc)
        return {"healthy": False, "url": "", "error": str(exc)}

    try:
        reachable, _version, msg = await cluster_probe.fetch_version(target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("k8s probe_target 探測失敗（%s）：%s", target.server, exc)
        return {"healthy": False, "url": target.server, "error": str(exc)}

    return {"healthy": reachable, "url": target.server, "message": msg}


async def fetch_info(conn: dict) -> dict[str, Any]:
    """回 dashboard 的版本／context 名／計數。

    version = 叢集 gitVersion（探測取得）；db_name = current-context 名
    （in-cluster 固定 "in-cluster"）；item_count 一律 None（k8s 無單一計數語意）。
    """
    info: dict[str, Any] = {"version": None, "db_name": None, "item_count": None}

    try:
        target = cluster_probe.resolve_target(conn)
    except ValueError:
        # 缺設定：db_name/version 皆 None，屬正常降級（非 bug）。
        return info
    except Exception as exc:  # noqa: BLE001
        logger.warning("k8s fetch_info 解析設定失敗：%s", exc)
        return info

    info["db_name"] = target.context_name

    try:
        _reachable, version, _msg = await cluster_probe.fetch_version(target)
        info["version"] = version
    except Exception as exc:  # noqa: BLE001
        logger.warning("k8s fetch_info 探版本失敗（%s）：%s", target.server, exc)

    return info
