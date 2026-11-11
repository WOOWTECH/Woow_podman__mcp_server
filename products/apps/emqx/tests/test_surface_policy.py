"""Public MCP surface; literal expected names are the released HA39 baseline."""
import json
import pytest
from fastmcp import Client
from emqx_mcp_server.server import build_server

EXPECTED = set('''emqx_cluster_status emqx_node_detail emqx_broker_stats emqx_metrics_current
emqx_metrics_history emqx_list_alarms emqx_prometheus_stats emqx_list_clients emqx_get_client
emqx_client_subscriptions emqx_kick_client emqx_client_subscribe emqx_client_unsubscribe
emqx_list_topics emqx_list_subscriptions emqx_publish emqx_publish_bulk emqx_list_retained
emqx_get_retained emqx_delete_retained emqx_list_authn emqx_manage_authn_users
emqx_list_authz_sources emqx_authz_settings emqx_manage_authz_rules emqx_list_banned
emqx_ban emqx_unban emqx_list_traces emqx_create_trace emqx_get_trace_log emqx_delete_trace
emqx_list_listeners emqx_list_rules emqx_get_rule_metrics emqx_toggle_rule emqx_test_rule_sql
emqx_list_connectors emqx_list_actions'''.split())

@pytest.fixture
def policy(tmp_path):
    p = tmp_path / 'config.json'
    p.write_text(json.dumps({'tools': {'disabled': [], 'disabled_operations': {}}}))
    p.chmod(0o600)
    return p

async def test_actual_mcp_lists_39_valid_schemas(policy, monkeypatch):
    from jsonschema import Draft202012Validator
    monkeypatch.setenv('WEBHOOK_SECURITY_MODE', 'moderate')
    async with Client(build_server(policy_path=policy)) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == EXPECTED
        assert len(tools) == 39
        for tool in tools:
            Draft202012Validator.check_schema(tool.input_schema)
            if tool.output_schema:
                Draft202012Validator.check_schema(tool.output_schema)
            assert tool.annotations is not None
            assert 'emqx' not in tool.input_schema.get('properties', {})

async def test_sql_test_is_not_advertised_read_only(policy):
    async with Client(build_server(policy_path=policy)) as client:
        tool = next(t for t in await client.list_tools() if t.name == 'emqx_test_rule_sql')
        assert tool.annotations.read_only_hint is False

@pytest.mark.parametrize('name', sorted(EXPECTED))
async def test_every_disabled_tool_refuses_before_backend_io(name, policy, broker):
    from test_39_calls import CASES
    arguments = next(args for short, args, _, _ in CASES if 'emqx_' + short == name)
    policy.write_text(json.dumps({'tools': {'disabled': [name], 'disabled_operations': {}}}))
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
        assert result.is_error
        assert 'EMQX_POLICY_DENIED' in result.content[0].text
        assert name not in {t.name for t in await client.list_tools()}
    assert broker.calls == []

@pytest.mark.parametrize('name', ['emqx_manage_authn_users', 'emqx_manage_authz_rules'])
@pytest.mark.parametrize('operation', [None, '', 'READ', ' read ', 'update', [], {}, True, 1])
async def test_mixed_selectors_are_strict_even_with_no_denies(name, operation, policy, broker):
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool(name, {'operation': operation}, raise_on_error=False)
        assert result.is_error
        assert 'EMQX_POLICY_DENIED' in result.content[0].text
    assert broker.calls == []

async def test_policy_revocation_and_enable_apply_in_same_mcp_session(policy, broker):
    async with Client(build_server(policy_path=policy)) as client:
        assert not (await client.call_tool('emqx_broker_stats')).is_error
        policy.write_text(json.dumps({'tools': {'disabled': ['emqx_broker_stats']}}))
        assert (await client.call_tool('emqx_broker_stats', raise_on_error=False)).is_error
        policy.write_text(json.dumps({'tools': {'disabled': []}}))
        assert not (await client.call_tool('emqx_broker_stats')).is_error
        assert not (await client.call_tool('emqx_cluster_status')).is_error
    assert len(broker.calls) == 3

@pytest.mark.parametrize('bad', ['{}', '{"tools": null}', '{"tools":{"disabled_operations":[]}}', 'not-json'])
async def test_corrupt_policy_never_allows_backend_call(bad, policy, broker):
    policy.write_text(bad)
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool('emqx_broker_stats', raise_on_error=False)
        assert result.is_error
        assert 'EMQX_POLICY_UNAVAILABLE' in result.content[0].text
    assert broker.calls == []

async def test_operation_denial_keeps_allowed_read_and_blocks_create(policy, broker):
    policy.write_text(json.dumps({'tools': {'disabled': [], 'disabled_operations': {
        'emqx_manage_authn_users': ['create', 'delete']}}}))
    async with Client(build_server(policy_path=policy)) as client:
        assert not (await client.call_tool('emqx_manage_authn_users', {'operation': 'read'})).is_error
        denied = await client.call_tool('emqx_manage_authn_users',
            {'operation': 'create', 'user_id': 'owned', 'password': 'owned-password'}, raise_on_error=False)
        assert denied.is_error
        policy.write_text(json.dumps({'tools': {'disabled': [], 'disabled_operations': {}}}))
        assert not (await client.call_tool('emqx_manage_authn_users',
            {'operation': 'create', 'user_id': 'owned', 'password': 'owned-password'})).is_error
    assert [c[0] for c in broker.calls] == ['GET', 'POST']


@pytest.mark.parametrize('kind', ['missing', 'symlink', 'hardlink', 'wide-mode', 'directory'])
async def test_unsafe_policy_never_creates_or_repairs_policy(kind, policy, broker):
    import os
    original = policy.read_bytes()
    backup = policy.with_name('saved.json')
    if kind == 'missing':
        policy.unlink()
    elif kind == 'symlink':
        policy.rename(backup)
        policy.symlink_to(backup)
    elif kind == 'hardlink':
        os.link(policy, backup)
    elif kind == 'wide-mode':
        policy.chmod(0o660)
    else:
        policy.unlink()
        policy.mkdir()
    async with Client(build_server(policy_path=policy)) as client:
        result = await client.call_tool('emqx_broker_stats', raise_on_error=False)
        assert result.is_error and 'EMQX_POLICY_UNAVAILABLE' in result.content[0].text
    assert broker.calls == []
    if kind == 'missing':
        assert not policy.exists()
    if kind in ('symlink', 'hardlink'):
        assert backup.read_bytes() == original
    if kind == 'wide-mode':
        assert policy.stat().st_mode & 0o777 == 0o660
