"""Product-specific URL normalization through config, child and actual HTTP paths."""

from management_client import revision_headers
import httpx
import pytest

from litellm_mcp_admin.health_probes import connection_target
from litellm_mcp_admin.routers.config import normalize_base_url
from woow_litellm_mcp_server.settings import Settings
from woow_litellm_mcp_server.tools import models
from .conftest import FakeMCP, build_gate, make_ctx, stored_config

CASES = [
    ('http://lite.test/', 'http://lite.test', '/v1/models'),
    ('http://lite.test/v1', 'http://lite.test', '/v1/models'),
    ('http://lite.test/v1///', 'http://lite.test', '/v1/models'),
    ('http://lite.test/proxy/v1/', 'http://lite.test/proxy/v1', '/proxy/v1/v1/models'),
    ('http://lite.test/proxy/', 'http://lite.test/proxy', '/proxy/v1/models'),
    ('http://lite.test/V1/', 'http://lite.test/V1', '/V1/v1/models'),
    ('http://lite.test/%76%31/', 'http://lite.test/%76%31', '/%76%31/v1/models'),
]


@pytest.mark.parametrize('raw,base,path', CASES)
async def test_config_settings_and_real_handler_preserve_mount(raw, base, path):
    assert normalize_base_url(raw) == base
    assert connection_target({'base_url': raw}) == (base, '')
    cfg = Settings(base_url=raw, _env_file=None)
    assert cfg.base_url == base
    seen = []
    def fake(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={'data': []})
    mcp = FakeMCP()
    models.register(mcp, build_gate())
    async with httpx.AsyncClient(base_url=cfg.base_url, transport=httpx.MockTransport(fake)) as client:
        assert await mcp.tools['litellm_list_models'](make_ctx(client)) == {'data': []}
    assert seen == [path]


@pytest.mark.parametrize('bad', ['http://user:pass@lite.test/v1', 'http://lite.test/v1?x=1',
                               'http://lite.test/v1#frag', 'http://lite.test:bad/v1',
                               'http://lite.test/a\\v1', 'ftp://lite.test/v1'])
def test_invalid_product_url_rejected_before_save(make_client, config_path, bad):
    client = make_client(connection={'base_url': 'http://keep.test', 'master_key': 'fake-key'})
    response = client.put('/api/config/connection', json={'base_url': bad, 'restart': False}, headers=revision_headers(client, None, read_path='/api/config'))
    assert response.status_code == 400
    assert stored_config(config_path)['connection']['base_url'] == 'http://keep.test'


def test_prefixed_mount_saved_without_secret_or_restart_change(make_client, config_path):
    client = make_client(connection={'base_url': 'http://old.test', 'master_key': 'fake-key'})
    response = client.put('/api/config/connection', json={'base_url': 'http://lite.test/proxy/v1/', 'restart': False}, headers=revision_headers(client, None, read_path='/api/config'))
    assert response.status_code == 200
    assert stored_config(config_path)['connection'] == {'base_url': 'http://lite.test/proxy/v1', 'master_key': 'fake-key'}
