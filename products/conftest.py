"""Isolate the source app's global ASGI session policy between test cases.

The upstream tests share one `app` and a few cases monkeypatch `app.state`.
Without a fresh policy, a patched policy can disappear after fixture teardown,
so later tests exercise legacy stateless login instead of Headscale login.
This test-only fixture does not change the vendored application.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_headscale_app_policy(request):
    if "apps/headscale/tests/" not in str(request.node.path):
        yield
        return
    from headscale_mcp_admin.admin_sessions import AdminSessions
    from headscale_mcp_admin.main import app

    original = getattr(app.state, "admin_session_policy", None)
    app.state.admin_session_policy = AdminSessions()
    try:
        yield
    finally:
        app.state.admin_session_policy = original or AdminSessions()
