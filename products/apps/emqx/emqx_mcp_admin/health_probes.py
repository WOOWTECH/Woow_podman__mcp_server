"""Authenticated broker identity probe using the same protected transport as tools."""
from emqx_mcp_server.http_policy import BrokerClient
from emqx_mcp_server.errors import emqx_request, json_body, EmqxApiError


async def check_emqx(conn):
    client = None
    try:
        client = BrokerClient(*(str(conn.get(k) or '') for k in ('base_url', 'api_key', 'api_secret')), request_timeout=5)
        body = json_body(await emqx_request(client, 'GET', '/nodes'))
        if (not isinstance(body, list) or not body or any(
            not isinstance(row, dict) or not isinstance(row.get('node'), str)
            or '@' not in row['node'] or not isinstance(row.get('version'), str)
            or not row['version'] for row in body)):
            return {'healthy': False, 'error': 'EMQX_INVALID_IDENTITY'}
        return {'healthy': True, 'version': body[0]['version'], 'item_count': len(body)}
    except EmqxApiError as exc:
        return {'healthy': False, 'error': str(exc)}
    except Exception:
        return {'healthy': False, 'error': 'EMQX_CONNECTION_UNAVAILABLE'}
    finally:
        if client is not None:
            await client.aclose()


async def probe_target(conn):
    return await check_emqx(conn)


async def fetch_info(conn):
    result = await check_emqx(conn)
    return {key: result.get(key) for key in ('version', 'item_count')}
