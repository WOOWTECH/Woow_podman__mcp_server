"""The real product connection handlers must report saved and applied separately."""
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

# apps/odoo-manage 已封存且移出 uv workspace（#78：它的 mcp-server-odoo 0.7.1 要求 mcp<2），不再測試。
APPS = ['hermes','n8n','odoo','k8s','vk','opendesign','litellm','nextcloud']

@pytest.mark.parametrize('app', APPS)
@pytest.mark.parametrize('outcome', [False, True, RuntimeError('not a public error')])
async def test_real_connection_handler_apply_result(app, outcome, monkeypatch, store_env):
    module = importlib.import_module(app + '_mcp_admin.routers.config')
    model = next(getattr(module, name) for name in ('ConnectionUpdateRequest', 'ConnectionUpdate', 'DualConnectionUpdate') if hasattr(module, name))
    supplied = {'restart': True, 'in_cluster': 'false', 'dashboard_username': 'parity',
                'odoo_username': 'parity', 'n8n_api_url': '', 'odoo_url': '',
                'base_url': '', 'od_api_base': '', 'vk_api_base': ''}
    values = {name: supplied[name] if name in supplied else '' for name, field in model.model_fields.items()
              if name in supplied or field.is_required()}
    request = model(**values)
    action = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    pm = SimpleNamespace(is_running=True, restart=action, start=action, start_or_restart=action)
    monkeypatch.setattr(module, 'get_config_store', lambda: store_env)
    monkeypatch.setattr(module, 'get_process_manager', lambda: pm)
    response = await module.update_connection(request)
    body = response if isinstance(response, dict) else response.model_dump()
    assert body['saved'] is True
    assert body['apply_state'] == ('applied' if outcome is True else 'failed')
    assert body['status'] == ('ok' if outcome is True else 'partial')
    assert body['restarted'] is (outcome is True)
    assert 'not a public error' not in str(body)
    action.assert_awaited_once()

@pytest.mark.parametrize('app', APPS)
async def test_save_only_does_not_claim_applied(app, monkeypatch, store_env):
    module = importlib.import_module(app + '_mcp_admin.routers.config')
    model = next(getattr(module, name) for name in ('ConnectionUpdateRequest', 'ConnectionUpdate', 'DualConnectionUpdate') if hasattr(module, name))
    values = {name: '' for name, field in model.model_fields.items() if field.is_required()}
    values.update(restart=False)
    if 'in_cluster' in model.model_fields: values['in_cluster'] = 'false'
    request = model(**values)
    monkeypatch.setattr(module, 'get_config_store', lambda: store_env)
    response = await module.update_connection(request)
    body = response if isinstance(response, dict) else response.model_dump()
    assert body['saved'] is True and body['apply_state'] == 'not_requested' and body['restarted'] is False
