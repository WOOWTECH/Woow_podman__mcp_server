"""Saved connection management through the shared revision/auth transition."""
from typing import Annotated
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from mcp_admin_core.config import get_config_store
from mcp_admin_core.process import get_process_manager
from mcp_admin_core.configuration import configuration_status, revision_header, run_configuration_transition
from mcp_admin_core.connection_apply import connection_outcome, refuse_blocked_urls
from emqx_mcp_server.http_policy import normalize_base_url, DestinationDenied
from .. import health_probes, log_redaction

router = APIRouter(prefix='/api/config', tags=['config'])
MASK = '********'


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    base_url: str = Field(default='', max_length=2048)
    api_key: str = Field(default='', max_length=1024)
    api_secret: str = Field(default='', max_length=4096)
    restart: bool = True


@router.get('')
async def get_connection(response: Response):
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    conn = snapshot.get('connection') or {}
    return {'configuration': configuration, 'base_url': conn.get('base_url', ''),
            **{f'{key}_{suffix}': (MASK if conn.get(key) else '(not set)') if suffix == 'masked' else bool(conn.get(key))
               for key in ('api_key', 'api_secret') for suffix in ('masked', 'set')}}


@router.put('/connection')
async def update_connection(req: ConnectionUpdate, request: Request, response: Response,
                            if_match: Annotated[str | None, Header(alias='If-Match')] = None):
    async def apply():
        updates = {}
        try:
            updates['base_url'] = normalize_base_url(req.base_url) if req.base_url.strip() else ''
        except DestinationDenied:
            raise HTTPException(400, 'EMQX_DESTINATION_DENIED') from None
        for key in ('api_key', 'api_secret'):
            value = getattr(req, key)
            if any(ord(c) < 32 or ord(c) == 127 for c in value) or key == 'api_key' and ':' in value:
                raise HTTPException(400, 'EMQX_INVALID_CREDENTIAL_FORMAT')
            if value and value != MASK:
                updates[key] = value
        await refuse_blocked_urls(updates, ['base_url'])
        merged = await get_config_store().patch('connection', updates)
        log_redaction.set_secrets(merged.get('api_key', ''), merged.get('api_secret', ''))
        pm, applied, started = get_process_manager(), False, False
        if req.restart:
            try:
                started = not pm.is_running
                applied = bool(await pm.start_or_restart())
            except Exception:
                pass  # Persistence succeeded; report application separately.
        return {'success': True, **connection_outcome(req.restart, applied, started=started and applied)}
    return await run_configuration_transition(get_config_store(), get_process_manager(),
                                               if_match, apply, response, request=request)


@router.post('/test')
async def test_connection():
    result = await health_probes.check_emqx(await get_config_store().get('connection', {}) or {})
    return {'success': result['healthy'], 'message': 'EMQX connected' if result['healthy'] else result['error'],
            'details': result}
