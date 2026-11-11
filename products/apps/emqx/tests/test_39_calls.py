"""All public tools through MCP, auth and real HTTP sockets to owned fixtures."""
import json
from urllib.parse import urlsplit
import pytest
from fastmcp import Client
from emqx_mcp_server.server import build_server
from test_surface_policy import EXPECTED

# Explicit expected API mappings, independent of adapter implementation.
CASES = [
 ('cluster_status', {}, 'GET', '/nodes'),
 ('node_detail', {'node': 'emqx@owned'}, 'GET', '/nodes/emqx@owned'),
 ('broker_stats', {}, 'GET', '/stats'),
 ('metrics_current', {}, 'GET', '/monitor_current'),
 ('metrics_history', {}, 'GET', '/monitor'),
 ('list_alarms', {}, 'GET', '/alarms'),
 ('prometheus_stats', {}, 'GET', '/prometheus/stats'),
 ('list_clients', {}, 'GET', '/clients'),
 ('get_client', {'clientid': 'owned'}, 'GET', '/clients/owned'),
 ('client_subscriptions', {'clientid': 'owned'}, 'GET', '/clients/owned/subscriptions'),
 ('kick_client', {'clientid': 'owned'}, 'DELETE', '/clients/owned'),
 ('client_subscribe', {'clientid': 'owned', 'topic': 'owned/topic'}, 'POST', '/clients/owned/subscribe'),
 ('client_unsubscribe', {'clientid': 'owned', 'topic': 'owned/topic'}, 'POST', '/clients/owned/unsubscribe'),
 ('list_topics', {}, 'GET', '/topics'),
 ('list_subscriptions', {}, 'GET', '/subscriptions'),
 ('publish', {'topic': 'owned/topic', 'payload': 'owned-message'}, 'POST', '/publish'),
 ('publish_bulk', {'messages': [{'topic': 'owned/topic', 'payload': 'owned-message'}]}, 'POST', '/publish/bulk'),
 ('list_retained', {}, 'GET', '/mqtt/retainer/messages'),
 ('get_retained', {'topic': 'owned/topic'}, 'GET', '/mqtt/retainer/message/owned%2Ftopic'),
 ('delete_retained', {'topic': 'owned/topic'}, 'DELETE', '/mqtt/retainer/message/owned%2Ftopic'),
 ('list_authn', {}, 'GET', '/authentication'),
 ('manage_authn_users', {'operation': 'read'}, 'GET', '/authentication/password_based%3Abuilt_in_database/users'),
 ('list_authz_sources', {}, 'GET', '/authorization/sources'),
 ('authz_settings', {}, 'GET', '/authorization/settings'),
 ('manage_authz_rules', {'operation': 'read'}, 'GET', '/authorization/sources/built_in_database/rules/users'),
 ('list_banned', {}, 'GET', '/banned'),
 ('ban', {'subject_type': 'clientid', 'who': 'owned'}, 'POST', '/banned'),
 ('unban', {'subject_type': 'clientid', 'who': 'owned'}, 'DELETE', '/banned/clientid/owned'),
 ('list_traces', {}, 'GET', '/trace'),
 ('create_trace', {'name': 'owned', 'target_type': 'clientid', 'target': 'owned'}, 'POST', '/trace'),
 ('get_trace_log', {'name': 'owned'}, 'GET', '/trace/owned/log_detail'),
 ('delete_trace', {'name': 'owned'}, 'DELETE', '/trace/owned'),
 ('list_listeners', {}, 'GET', '/listeners'),
 ('list_rules', {}, 'GET', '/rules'),
 ('get_rule_metrics', {'rule_id': 'owned'}, 'GET', '/rules/owned/metrics'),
 ('toggle_rule', {'rule_id': 'owned', 'enable': False}, 'PUT', '/rules/owned'),
 ('test_rule_sql', {'sql': 'SELECT * FROM "owned/#"'}, 'POST', '/rule_test'),
 ('list_connectors', {}, 'GET', '/connectors'),
 ('list_actions', {}, 'GET', '/actions'),
]


def test_matrix_covers_exact_39():
    assert {'emqx_' + c[0] for c in CASES} == EXPECTED


@pytest.mark.parametrize('name,args,method,path', CASES, ids=[c[0] for c in CASES])
async def test_allowed_tool_dispatch_and_output_schema(name, args, method, path, broker, tmp_path):
    from jsonschema import validate
    policy = tmp_path / 'config.json'
    policy.write_text('{"tools":{"disabled":[]}}')
    policy.chmod(0o600)
    broker.routes = {
        '/api/v5/authorization/sources': {'sources': [{'type': 'built_in_database'}]},
        '/api/v5/publish': {'id': 'owned-id', 'reason_code': 0},
        '/api/v5/publish/bulk': [{'id': 'owned-id', 'reason_code': 0}],
        '/api/v5/mqtt/retainer/message/owned%2Ftopic': {'payload': 'b3duZWQ=', 'qos': 0},
        '/api/v5/trace/owned/log_detail': [],
    }
    async with Client(build_server(policy_path=policy)) as client:
        schema = next(t.output_schema for t in await client.list_tools() if t.name == 'emqx_' + name)
        result = await client.call_tool('emqx_' + name, args, raise_on_error=False)
        assert not result.is_error, result
        validate(result.structured_content, schema)
    assert (broker.calls[-1][0], urlsplit(broker.calls[-1][1]).path) == (method, '/api/v5' + path)
    assert len(broker.calls) == (2 if name == 'manage_authz_rules' else 1)
    if method in ('POST', 'PUT'):
        body = json.loads(broker.calls[-1][2])
        assert body  # The authenticated request carries a real payload.
        if name == 'publish':
            assert body['payload'] == 'owned-message' and body['topic'] == 'owned/topic'
        if name == 'toggle_rule':
            assert body == {'enable': False}
