"""Exact reviewed method settings. The upstream switches and ACLs still apply."""
from __future__ import annotations

import json
import os
import re
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from mcp_admin_core.config import get_config_store
from mcp_admin_core.configuration import (
    configuration_status,
    revision_header,
    run_configuration_transition,
)
from mcp_admin_core.process import get_process_manager
from pydantic import BaseModel, BeforeValidator, ConfigDict

from .seed_defaults import write_defaults

FIELD = 'odoo_allowed_methods'
ENV = 'ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS'
MAX_METHODS = 128
MAX_ENTRY_LENGTH = 256
MAX_BYTES = 16384
EXACT_METHOD = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.[A-Za-z][A-Za-z0-9_]*')
router = APIRouter(prefix='/api/odoo/allowed-methods', tags=['config'])


def validate_methods(value):
    if (not isinstance(value, list) or len(value) > MAX_METHODS
            or any(not isinstance(item, str) or len(item) > MAX_ENTRY_LENGTH
                   or not EXACT_METHOD.fullmatch(item) for item in value)
            or len(set(value)) != len(value)
            or len(json.dumps(value, separators=(',', ':'))) > MAX_BYTES):
        raise ValueError('invalid Odoo allowed methods')
    return list(value)


class MethodUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    odoo_allowed_methods: Annotated[list[str], BeforeValidator(validate_methods)]


def child_env(snapshot):
    """Keep inherited container policy until explicitly saved; [] overrides it."""
    result = write_defaults(snapshot)
    server = snapshot.get('mcp_server') or {}
    if FIELD in server:
        result[ENV] = ','.join(validate_methods(server[FIELD]))
    return result


def view(snapshot):
    server = snapshot.get('mcp_server') or {}
    if FIELD in server:
        methods, source = validate_methods(server[FIELD]), 'saved'
    else:
        # Upstream strips and deduplicates deployment env. Reading never persists or normalizes it.
        methods = list(dict.fromkeys(item.strip() for item in os.environ.get(ENV, '').split(',') if item.strip()))
        source = 'container_environment' if methods else 'legacy_empty'
    return {FIELD: methods, FIELD + '_source': source}


@router.get('')
async def get_methods(response: Response):
    store = get_config_store()
    snapshot = await store.load()
    configuration = configuration_status(store, snapshot, get_process_manager())
    revision_header(response, configuration)
    return {**view(snapshot), 'configuration': configuration}


@router.put('')
async def put_methods(body: MethodUpdate, request: Request, response: Response,
                      if_match: Annotated[str | None, Header(alias='If-Match')] = None):
    if if_match is None:
        raise HTTPException(428, detail={'code': 'precondition_required', 'message': '請重新載入設定後再儲存。'})
    store, pm = get_config_store(), get_process_manager()

    async def apply_update():
        # The shared edit revision already covers mcp_server. Keep this product-only
        # setting there so every settings editor sees conflicts without a core change.
        await store.patch('mcp_server', {FIELD: body.odoo_allowed_methods})
        try:
            applied = bool(await pm.start_or_restart())
        except Exception:  # noqa: BLE001 — saved config must report apply failure without leaking child output
            applied = False
        return {'saved': True, 'status': 'ok' if applied else 'partial',
                'apply_state': 'applied' if applied else 'failed',
                'message': '方法清單已儲存並套用；後端連線仍需另行驗證。' if applied else
                           '方法清單已儲存，但子程序未能啟動。請檢查服務狀態後重試。'}

    return await run_configuration_transition(store, pm, if_match, apply_update, response, request=request)
