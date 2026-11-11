"""PaaS EMQX app composition; authentication and N13/N15 stay in shared core."""
from contextlib import asynccontextmanager
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp_admin_core.app import create_app
from mcp_admin_core.config import get_config_store
from mcp_admin_core.profiles import get_profile_registry
from mcp_admin_core.launch_policy import LaunchPolicy
from mcp_admin_core.routers.health import make_health_router
from mcp_admin_core.routers.logs import create_logs_router
from mcp_admin_core.routers.tools import create_tools_router
from . import health_probes, log_redaction, tool_registry
from .routers import config

LAUNCH_POLICY = LaunchPolicy(
    command='emqx-mcp-server', args=('--port', '{port}'), env_prefix='EMQX_MCP_',
    connection_keys=frozenset({'base_url', 'api_key', 'api_secret'}),
    url_connection_keys=frozenset({'base_url'}),
    required_connection=(('base_url', 'EMQX URL'), ('api_key', 'API key'), ('api_secret', 'API secret')),
)

app = create_app(title='EMQX MCP Admin', product_id='emqx', deployment_kind='podman',
    launch_policy=LAUNCH_POLICY, tool_registry=tool_registry,
    extra_routers=[config.router,
        create_tools_router(registry=tool_registry, mcp_label='emqx-mcp'),
        make_health_router(app_type='emqx', probe_target=health_probes.probe_target, fetch_info=health_probes.fetch_info),
        create_logs_router('emqx_mcp_admin')])
log_redaction.install()
app.state.connection_saved = lambda conn: log_redaction.set_secrets(
    conn.get('api_key', ''), conn.get('api_secret', ''))
_core_lifespan = app.router.lifespan_context


@app.exception_handler(RequestValidationError)
async def invalid_input(request, exc):
    # Pydantic's structured errors otherwise include the supplied credential.
    return JSONResponse(status_code=422, content={'detail': 'EMQX_INVALID_REQUEST'})


@asynccontextmanager
async def lifespan(a):
    registry = get_profile_registry()
    for profile_id in ('default', *await registry.index()):
        with registry.activate(profile_id):
            conn = await get_config_store().get('connection', {}) or {}
        log_redaction.set_secrets(conn.get('api_key', ''), conn.get('api_secret', ''))
    async with _core_lifespan(a):
        yield


app.router.lifespan_context = lifespan
