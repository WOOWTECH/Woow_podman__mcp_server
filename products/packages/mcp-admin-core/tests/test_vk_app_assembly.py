"""vk main.py 組裝測試：title 與各 router 掛載路徑。"""
from __future__ import annotations


def test_vk_app_title_and_routes_mounted():
    from vk_mcp_admin.main import app
    assert app.title == "VK MCP Admin"

    # 遞迴蒐集掛載路徑（starlette include_router 包 _IncludedRouter，
    # 比照 tests/test_app_factory.py 的 _collect_paths 寫法）
    def _collect_paths(routes) -> set[str]:
        paths: set[str] = set()
        for r in routes:
            p = getattr(r, "path", None)
            if p:
                paths.add(p)
            orig = getattr(r, "original_router", None)
            if orig is not None and hasattr(orig, "routes"):
                paths |= _collect_paths(orig.routes)
            elif not p and hasattr(r, "routes"):
                paths |= _collect_paths(r.routes)
        return paths

    paths = _collect_paths(app.routes)
    for expected in ("/healthz", "/api/config", "/api/config/connection", "/api/config/test",
                     "/api/tools", "/api/health", "/api/logs/stream", "/api/tokens"):
        assert expected in paths, expected
