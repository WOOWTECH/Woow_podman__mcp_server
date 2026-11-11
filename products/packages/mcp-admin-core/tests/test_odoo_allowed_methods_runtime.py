"""Managed lists reach the actual patched pinned child; only fake Odoo receives calls."""
import pytest
from _odoo_child import build_patched_site, payload, run_child
from odoo_mcp_admin.allowed_methods import child_env


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    return build_patched_site(tmp_path_factory.mktemp('odoo-methods-site'))


@pytest.mark.parametrize('off', [None, 'ODOO_MCP_ENABLE_WRITES', 'ODOO_MCP_ALLOW_DIRECT_WRITES', 'MCP_CHATTER_DIRECT'])
def test_managed_exact_methods_keep_switch_precedence(site, tmp_path, off):
    switches = {'ODOO_MCP_ENABLE_WRITES': '1', 'ODOO_MCP_ALLOW_DIRECT_WRITES': '1',
                'ODOO_MCP_ALLOW_UNKNOWN_METHODS': '0', 'MCP_CHATTER_DIRECT': '1'}
    if off:
        switches[off] = '0'
    methods = ['sale.order.action_confirm', 'res.partner.message_post', 'res.partner.create']
    snapshot = {'mcp_server': {'env': switches, 'odoo_allowed_methods': methods}}
    calls = [('sale.order', 'action_confirm', [[1]]), ('sale.order2', 'action_confirm', [[1]]),
             ('sale.order', 'action_confirm_extra', [[1]]), ('sale.order', 'Action_confirm', [[1]]),
             ('res.partner', 'message_post', [[1]]), ('res.partner', 'create', [{'name': 'FAKE'}])]

    async def scenario(call, rpc):
        return [payload(await call('execute_method', {'model': model, 'method': method, 'args': args}))
                for model, method, args in calls]

    run = run_child(site, child_env(snapshot), tmp_path, scenario)
    enable = off != 'ODOO_MCP_ENABLE_WRITES'
    expected = [enable, False, False, False, enable and off != 'MCP_CHATTER_DIRECT',
                enable and off != 'ODOO_MCP_ALLOW_DIRECT_WRITES']
    assert [result['success'] for result in run['result']] == expected
    assert [(call['model'], call['method']) for call in run['odoo'].calls] == [
        (model, method) for (model, method, _), permitted in zip(calls, expected) if permitted]
