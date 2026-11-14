"""Startup must never replay an authenticated initialize across child generations."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import httpx
import pytest
from mcp_admin_core.process import McpProcessManager

TOKEN = 'synthetic-startup-token'
INIT = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'},
}}


class Manager(McpProcessManager):
    def __init__(self):
        super().__init__()
        self._process = SimpleNamespace(pid=12345, returncode=None)
        self._running = True
        self._verifying_configuration = True
        # Synthetic stand-in for the probe's OS listener check (MockTransport: no socket to inspect).
        self._peer_verified = (self._initialize_generation_changed, 59999)

    @property
    def child_pid(self):
        return self._process.pid

    @child_pid.setter
    def child_pid(self, value):
        self._process.pid = value


def gateway(make_app, monkeypatch, handler):
    from mcp_admin_core import proxy
    async def no_placeholder(*args, **kwargs):
        return None
    pm = Manager()
    monkeypatch.setattr(proxy, 'get_process_manager', lambda: pm)
    monkeypatch.setattr(proxy.setup_status, 'unreachable_answer', no_placeholder)
    monkeypatch.setattr(proxy, '_get_client', lambda timeout=86400: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    c = make_app({'mcp_auth_token': TOKEN, 'mcp_server': {'command': '', 'args': [], 'port': 59999, 'env': {}}})
    return c, pm


def test_startup_connect_error_returns_retryable_503_without_replay(make_app, monkeypatch):
    sent=[]
    async def upstream(request):
        sent.append(request)
        if len(sent)==1:
            raise httpx.ConnectError('synthetic refused', request=request)
        return httpx.Response(200, json={'jsonrpc':'2.0','id':1,'result':{}})
    client,_=gateway(make_app, monkeypatch, upstream)
    response=client.post('/private_'+TOKEN+'/mcp',json=INIT)
    assert response.status_code==503 and response.headers['retry-after']=='1' and len(sent)==1


def test_change_of_child_at_async_boundary_cannot_receive_internal_retry(make_app, monkeypatch):
    sent=[]
    pm=None
    async def upstream(request):
        sent.append(pm.child_pid)
        asyncio.get_running_loop().call_soon(setattr,pm,'child_pid',99999)
        raise httpx.ConnectError('synthetic refused', request=request)
    client,pm=gateway(make_app,monkeypatch,upstream)
    response=client.post('/private_'+TOKEN+'/mcp',json=INIT)
    assert response.status_code==503 and sent==[12345]


def test_verification_end_cannot_trigger_an_extra_send(make_app, monkeypatch):
    sent=[]
    pm=None
    async def upstream(request):
        sent.append(1)
        pm._verifying_configuration=False
        raise httpx.ConnectError('synthetic refused',request=request)
    client,pm=gateway(make_app,monkeypatch,upstream)
    response=client.post('/private_'+TOKEN+'/mcp',json=INIT)
    assert response.status_code==502 and len(sent)==1


@pytest.mark.parametrize('body',[
    {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'write'}},
    [INIT,{'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'write'}}],
    {'jsonrpc':'2.0','method':'notifications/initialized'},
])
def test_non_initialize_requests_are_not_replayed(make_app,monkeypatch,body):
    sent=[]
    async def upstream(request):
        sent.append(1)
        raise httpx.ConnectError('synthetic refused',request=request)
    client,_=gateway(make_app,monkeypatch,upstream)
    response=client.post('/private_'+TOKEN+'/mcp',json=body)
    assert response.status_code==502 and len(sent)==1


def test_revocation_during_connect_returns_403(make_app,monkeypatch):
    from mcp_admin_core.config.store import get_config_store
    sent=[]
    async def upstream(request):
        sent.append(1)
        await get_config_store().change_mcp_token(None,action='revoke')
        raise httpx.ConnectError('synthetic refused',request=request)
    client,_=gateway(make_app,monkeypatch,upstream)
    response=client.post('/private_'+TOKEN+'/mcp',json=INIT)
    assert response.status_code==403 and len(sent)==1


def test_upstream_http_error_is_not_replayed(make_app,monkeypatch):
    sent=[]
    async def upstream(request):
        sent.append(1)
        return httpx.Response(503,json={'error':'synthetic'})
    client,_=gateway(make_app,monkeypatch,upstream)
    response=client.post('/private_'+TOKEN+'/mcp',json=INIT)
    assert response.status_code==503 and len(sent)==1


def test_successful_initialize_is_unchanged(make_app,monkeypatch):
    sent=[]
    async def upstream(request):
        sent.append(1)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':1,'result':{}},headers={'mcp-session-id':'synthetic'})
    client,_=gateway(make_app,monkeypatch,upstream)
    response=client.post('/private_'+TOKEN+'/mcp',json=INIT)
    assert response.status_code==200 and response.headers['mcp-session-id']=='synthetic' and len(sent)==1


def test_client_can_retry_in_a_separate_authenticated_request(make_app, monkeypatch):
    sent=[]
    async def upstream(request):
        sent.append(1)
        if len(sent)==1:
            raise httpx.ConnectError('synthetic refused',request=request)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':1,'result':{}},headers={'mcp-session-id':'synthetic'})
    client,_=gateway(make_app,monkeypatch,upstream)
    first=client.post('/private_'+TOKEN+'/mcp',json=INIT)
    assert first.status_code==503 and len(sent)==1
    second=client.post('/private_'+TOKEN+'/mcp',json=INIT)
    assert second.status_code==200 and len(sent)==2


@pytest.mark.parametrize('error',[httpx.ConnectTimeout,httpx.ReadTimeout,httpx.ReadError,httpx.WriteError])
def test_timeouts_and_post_connect_errors_are_not_replayed(make_app,monkeypatch,error):
    sent=[]
    async def upstream(request):
        sent.append(1)
        raise error('synthetic',request=request)
    client,_=gateway(make_app,monkeypatch,upstream)
    try:client.post('/private_'+TOKEN+'/mcp',json=INIT)
    except error:pass
    assert len(sent)==1


@pytest.mark.asyncio
async def test_cancellation_never_creates_an_extra_send(make_app,monkeypatch):
    sent=[]
    started=asyncio.Event()
    async def upstream(request):
        sent.append(1);started.set()
        await asyncio.sleep(20)
        return httpx.Response(200)
    client,_=gateway(make_app,monkeypatch,upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),base_url='http://test') as c:
        task=asyncio.create_task(c.post('/private_'+TOKEN+'/mcp',json=INIT))
        await asyncio.wait_for(started.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        await asyncio.sleep(.05)
    assert len(sent)==1
