"""子行程身分（blocker 3）：真的子程序、真的 loopback TCP，閘道經 ASGI 轉發。

全部使用合成的 MCP 程序與合成 token；只綁 127.0.0.1 的臨時 port，每個測試結束前停止自己 spawn 的程序。
- 受管子行程（managed-positive）照常回答；事先或事後佔住 port 的程序（unmanaged-negative、競爭）永遠收不到
  任何 MCP 請求，世代被拒、MCP 請求走既有的 not-running／setup 答案或 503。
- 子行程 fork 出來綁 port 的孫行程：停止時整個 process group 結束、port 釋放、新的子行程綁得到。
- 管理程序被 SIGKILL（Linux）：子行程在幾秒內消失（PR_SET_PDEATHSIG）；沒有 parent-death signal 的對照程序存活。
- 與其他 profile 或管理程序 listener 相同的 port：拒絕 spawn。
- 已驗證的子行程不再聽 port、轉發連不上：重新檢查後隔離世代，之後佔住 port 的程序也收不到請求。
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mcp_admin_core import proxy
from mcp_admin_core.configuration import configuration_status
from mcp_admin_core.config.store import get_config_store
from mcp_admin_core.process import McpProcessManager

LINUX = sys.platform.startswith('linux')
TOKEN = 'synthetic-child-identity-token'
INIT = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'synthetic', 'version': '1'}}}
LIST = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}
HEADERS = {'Accept': 'application/json, text/event-stream'}

# Synthetic MCP peer: answers initialize with "<marker>-<pid>", appends every method to a log file, and on
# SIGUSR1 closes its listener but stays alive.
SERVER = r'''import json, os, signal, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
port, marker, ready, log = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_DELETE(self):
        self.send_response(204); self.end_headers()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        method = body.get('method')
        with log.open('a') as handle:
            handle.write(str(method) + '\n')
        if method == 'notifications/initialized':
            self.send_response(202); self.end_headers(); return
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {},
                      'serverInfo': {'name': marker + '-' + str(os.getpid()), 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': []}
        else:
            result = {}
        raw = json.dumps({'jsonrpc': '2.0', 'id': body.get('id'), 'result': result}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
server = HTTPServer(('127.0.0.1', port), Handler)
def close_listener(*_):
    threading.Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True).start()
signal.signal(signal.SIGUSR1, close_listener)
ready.write_text(str(os.getpid()))
server.serve_forever()
time.sleep(3600)
'''
# Direct child that forks the server as a grandchild (same session and process group) and only sleeps.
FORKER = r'''import subprocess, sys, time
subprocess.Popen([sys.executable] + sys.argv[1:])
time.sleep(3600)
'''
SLEEPER = ['-c', 'import time; time.sleep(3600)']


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def port_free(port):
    """A listener-style bind (SO_REUSEADDR) to 127.0.0.1:port succeeds."""
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def alive(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ('Z', 'X')


def same_process(pid, marker):
    """True only while *pid* is still one of this test's synthetic processes (guards cleanup against PID reuse)."""
    try:
        return marker.encode() in Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return False


async def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


class Peers:
    """Synthetic peer processes started by this test (rivals, or their pid files); stopped in teardown."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.source = tmp_path / 'peer.py'
        self.source.write_text(SERVER)
        (tmp_path / 'forker.py').write_text(FORKER)
        self.rivals = []
        self.pid_files = []
        self.tracked = []

    def track(self, pid):
        self.tracked.append(pid)

    def args(self, port, marker, forker=False):
        ready, log = self.tmp / f'{marker}.ready', self.tmp / f'{marker}.log'
        self.pid_files.append(ready)
        args = [str(self.source), str(port), marker, str(ready), str(log)]
        return ([str(self.tmp / 'forker.py')] + args if forker else args), ready, log

    async def rival(self, port, marker='rival-synthetic'):
        args, ready, log = self.args(port, marker)
        proc = subprocess.Popen([sys.executable, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        self.rivals.append(proc)
        assert await wait_until(lambda: ready.exists() or proc.poll() is not None)
        assert proc.poll() is None and ready.exists()
        return log

    def cleanup(self):
        for proc in self.rivals:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)  # own new session: pgid == pid, process not reaped yet
            proc.wait(timeout=5)
        pids = list(self.tracked)
        for ready in self.pid_files:
            try:
                pids.append(int(ready.read_text()))
            except (OSError, ValueError):
                continue
        for pid in pids:
            if alive(pid) and same_process(pid, str(self.tmp)):
                os.kill(pid, signal.SIGKILL)


@pytest.fixture
def peers(tmp_path):
    p = Peers(tmp_path)
    yield p
    p.cleanup()


def gateway(make_app, monkeypatch, port, args, **overrides):
    app = make_app({'mcp_auth_token': TOKEN, 'mcp_server': {'command': sys.executable, 'args': args,
                                                           'port': port, 'env': {}}, **overrides}).app
    pm = McpProcessManager()
    pm.auto_restart_delays = (30,)
    monkeypatch.setattr(proxy, 'get_process_manager', lambda: pm)
    monkeypatch.setattr(proxy, '_client', None)
    return app, pm


async def post(app, message):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        return await client.post('/private_' + TOKEN + '/mcp', json=message, headers=HEADERS)


async def stop(pm):
    await asyncio.wait_for(pm.stop(), 20)


def refused_without_forwarding(response, *, rival_log):
    """The MCP request got the gateway's own not-running/setup answer or a 503, and the rival saw nothing."""
    assert 'rival-synthetic' not in response.text
    assert not rival_log.exists() or rival_log.read_text() == ''
    if response.status_code == 200:
        result = response.json()['result']
        if 'serverInfo' in result:
            assert result['serverInfo']['name'].endswith('not running）')
        else:
            assert [tool['name'] for tool in result['tools']] == ['setup_status']
    else:
        assert response.status_code in (502, 503)


async def last_error(pm):
    store = get_config_store()
    return configuration_status(store, await store.load(), pm)


@pytest.mark.parametrize('unmanaged', [False, True], ids=['managed-positive', 'unmanaged-negative'])
async def test_only_managed_child_may_answer_initialize(make_app, monkeypatch, peers, unmanaged):
    port = free_port()
    rival_log = None
    if unmanaged:
        rival_log = await peers.rival(port)
        args = SLEEPER
    else:
        args, ready, _log = peers.args(port, 'managed-synthetic')
    app, pm = gateway(make_app, monkeypatch, port, args)
    try:
        started = await pm.start()
        probe = pm._configuration_task
        if probe is not None:
            await asyncio.wait_for(probe, 15)
        response = await post(app, INIT)
        if not unmanaged:
            assert started and pm._applied_revision == pm._attempted_revision
            assert response.status_code == 200
            assert response.json()['result']['serverInfo']['name'] == f'managed-synthetic-{pm._process.pid}'
            return
        # The port was already taken: the generation is refused before spawn, nothing reaches the rival.
        assert not started and not pm.is_running
        status = await last_error(pm)
        assert status['apply_state'] == 'failed' and status['last_error_code'] == 'child_port_in_use'
        refused_without_forwarding(response, rival_log=rival_log)
        refused_without_forwarding(await post(app, LIST), rival_log=rival_log)
    finally:
        await stop(pm)


@pytest.mark.skipif(not LINUX, reason='needs /proc to see who owns the listener')
async def test_listener_bound_after_spawn_by_another_process_is_never_used(make_app, monkeypatch, peers):
    """Check-then-bind race: the child never binds; another process takes the port after the spawn check."""
    port = free_port()
    app, pm = gateway(make_app, monkeypatch, port, SLEEPER)
    try:
        assert await pm.start()
        probe = pm._configuration_task
        child = pm._process
        rival_log = await peers.rival(port)
        await asyncio.wait_for(probe, 15)
        assert await wait_until(lambda: child.returncode is not None)  # the refused generation is stopped
        status = await last_error(pm)
        assert status['apply_state'] == 'failed' and status['last_error_code'] == 'child_peer_mismatch'
        refused_without_forwarding(await post(app, INIT), rival_log=rival_log)
        refused_without_forwarding(await post(app, LIST), rival_log=rival_log)
    finally:
        await stop(pm)


@pytest.mark.skipif(not LINUX, reason='needs /proc to identify the grandchild')
async def test_stop_kills_grandchild_that_holds_the_port_and_a_new_child_can_bind(make_app, monkeypatch, peers):
    port = free_port()
    args, ready, _log = peers.args(port, 'grandchild-synthetic', forker=True)
    app, pm = gateway(make_app, monkeypatch, port, args)
    try:
        assert await pm.start()
        await asyncio.wait_for(pm._configuration_task, 15)
        grandchild = int(ready.read_text())
        peers.track(grandchild)
        assert grandchild != pm._process.pid
        response = await post(app, INIT)
        assert response.status_code == 200
        assert response.json()['result']['serverInfo']['name'] == f'grandchild-synthetic-{grandchild}'

        await stop(pm)
        assert await wait_until(lambda: not alive(grandchild), 5)
        assert await wait_until(lambda: port_free(port), 5)

        ready.unlink()
        assert await pm.start()
        await asyncio.wait_for(pm._configuration_task, 15)
        replacement = int(ready.read_text())
        response = await post(app, INIT)
        assert response.status_code == 200
        assert response.json()['result']['serverInfo']['name'] == f'grandchild-synthetic-{replacement}'
    finally:
        await stop(pm)


@pytest.mark.skipif(not LINUX, reason='needs /proc to identify the grandchild')
async def test_crashed_child_leaves_no_grandchild_on_the_port(make_app, monkeypatch, peers):
    port = free_port()
    args, ready, _log = peers.args(port, 'crash-synthetic', forker=True)
    app, pm = gateway(make_app, monkeypatch, port, args)
    pm.auto_restart_delays = (0.2,)
    try:
        assert await pm.start()
        await asyncio.wait_for(pm._configuration_task, 15)
        first, grandchild = pm._process, int(ready.read_text())
        peers.track(grandchild)
        ready.unlink()
        first.kill()  # the direct child dies; its grandchild still holds the port
        assert await wait_until(lambda: not alive(grandchild), 5)
        assert await wait_until(lambda: pm.is_running and pm._process is not first and ready.exists(), 15)
        await asyncio.wait_for(pm._configuration_task or asyncio.sleep(0), 15)
        replacement = int(ready.read_text())
        response = await post(app, INIT)
        assert response.status_code == 200
        assert response.json()['result']['serverInfo']['name'] == f'crash-synthetic-{replacement}'
    finally:
        await stop(pm)


HARNESS = r'''import asyncio, json, os, subprocess, sys
from pathlib import Path
from mcp_admin_core.process import McpProcessManager
async def main():
    pm = McpProcessManager()
    assert await pm.start()
    # Control: a plain child with no parent-death signal, in its own session like the managed one.
    control = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'], start_new_session=True)
    Path(sys.argv[1]).write_text(json.dumps({'managed': pm._process.pid, 'control': control.pid}))
    await asyncio.sleep(3600)
if sys.argv[2] == 'uvloop':
    import uvloop
    uvloop.run(main())
else:
    asyncio.run(main())
'''


@pytest.mark.skipif(not LINUX, reason='PR_SET_PDEATHSIG is Linux-only')
@pytest.mark.parametrize('loop', ['asyncio', 'uvloop'])  # production uvicorn (--loop auto) runs uvloop
def test_managed_child_dies_with_its_manager(tmp_path, loop):
    if loop == 'uvloop':
        pytest.importorskip('uvloop')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'mcp_server': {'command': sys.executable, 'args': SLEEPER,
                                                 'port': free_port(), 'env': {}}}))
    pids_file = tmp_path / 'pids.json'
    env = {**os.environ, 'MCP_ADMIN_CONFIG': str(config)}
    harness = subprocess.Popen([sys.executable, '-c', HARNESS, str(pids_file), loop], env=env, start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and harness.poll() is None and not pids_file.exists():
            time.sleep(0.05)
        assert harness.poll() is None and pids_file.exists()
        pids = json.loads(pids_file.read_text())
        assert alive(pids['managed']) and alive(pids['control'])
        os.kill(harness.pid, signal.SIGKILL)
        harness.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and alive(pids['managed']):
            time.sleep(0.05)
        assert not alive(pids['managed'])
        # The control proves the harness death really orphaned its children.
        assert alive(pids['control'])
    finally:
        if harness.poll() is None:
            os.killpg(harness.pid, signal.SIGKILL)
            harness.wait(timeout=5)
        for pid in (pids or {}).values():
            if alive(pid) and same_process(pid, 'time.sleep(3600)'):
                os.kill(pid, signal.SIGKILL)


async def test_port_assigned_to_another_profile_is_refused(make_app, monkeypatch):
    from mcp_admin_core.profiles import get_profile_registry
    default_port = free_port()
    make_app({'mcp_auth_token': TOKEN, 'mcp_server': {'command': sys.executable, 'args': SLEEPER,
                                                     'port': default_port, 'env': {}}})
    registry = get_profile_registry()
    first = await registry.create('Synthetic port A')
    second = await registry.create('Synthetic port B')
    assert len({default_port, first['port'], second['port']}) == 3
    # A restored or hand-edited profile config points its child at another profile's (or the default) port.
    for target in (second['port'], default_port):
        await registry.store_for(first['id']).patch('mcp_server', {'port': target})
        pm = registry.manager_for(first['id'])
        with registry.activate(first['id']):
            try:
                assert await pm.start() is False and not pm.is_running
                status = configuration_status(registry.store_for(first['id']),
                                              await registry.store_for(first['id']).load(), pm)
                assert status['last_error_code'] == 'child_port_conflict'
            finally:
                await stop(pm)


async def test_port_of_another_profiles_running_child_is_refused(make_app, monkeypatch):
    """The default profile's port is still in use by a profile child launched from an older configuration."""
    import mcp_admin_core.process as process_mod
    from mcp_admin_core.process import get_default_process_manager
    from mcp_admin_core.profiles import get_profile_registry
    monkeypatch.setattr(process_mod, '_instance', McpProcessManager())
    port = free_port()
    make_app({'mcp_auth_token': TOKEN, 'mcp_server': {'command': sys.executable, 'args': SLEEPER,
                                                     'port': port, 'env': {}}})
    registry = get_profile_registry()
    other = registry.manager_for((await registry.create('Synthetic running'))['id'])
    other._process = SimpleNamespace(pid=os.getpid(), returncode=None)  # synthetic: no OS process is signalled
    other._running = True
    other._active_launch = (sys.executable, port)
    pm = get_default_process_manager()
    try:
        assert await pm.start() is False and not pm.is_running
        assert (await last_error(pm))['last_error_code'] == 'child_port_conflict'
    finally:
        other._process = None
        await stop(pm)


async def test_admin_listener_port_is_refused(make_app, monkeypatch):
    admin = socket.socket()
    admin.bind(('127.0.0.1', 0))
    admin.listen()
    try:
        for port, argv in ((admin.getsockname()[1], None), (free_port(), 'cli')):
            if argv == 'cli':
                # During lifespan uvicorn has not bound its port yet; its command line says which one it will use.
                monkeypatch.setattr(sys, 'argv', ['/srv/venv/bin/uvicorn', 'app:app', '--host', '0.0.0.0',
                                                  '--port', str(port)])
            _app, pm = gateway(make_app, monkeypatch, port, SLEEPER)
            try:
                assert await pm.start() is False and not pm.is_running
                status = await last_error(pm)
                assert status['apply_state'] == 'failed' and status['last_error_code'] == 'child_port_conflict'
            finally:
                await stop(pm)
    finally:
        admin.close()


@pytest.mark.skipif(not LINUX, reason='needs /proc to re-check the listener')
async def test_verified_child_that_stops_listening_is_fenced_before_another_process_takes_the_port(
        make_app, monkeypatch, peers):
    port = free_port()
    args, ready, child_log = peers.args(port, 'closing-synthetic')
    app, pm = gateway(make_app, monkeypatch, port, args)
    try:
        assert await pm.start()
        await asyncio.wait_for(pm._configuration_task, 15)
        child = pm._process
        assert (await post(app, INIT)).status_code == 200
        os.kill(child.pid, signal.SIGUSR1)  # closes its listener, stays alive
        assert await wait_until(lambda: port_free(port), 5)
        failed = await post(app, LIST)  # connect fails -> the gateway re-checks who owns the port
        assert failed.status_code in (200, 502, 503)
        assert await wait_until(lambda: child.returncode is not None, 10)
        assert (await last_error(pm))['last_error_code'] == 'child_peer_lost'
        rival_log = await peers.rival(port)
        refused_without_forwarding(await post(app, LIST), rival_log=rival_log)
        refused_without_forwarding(await post(app, INIT), rival_log=rival_log)
    finally:
        await stop(pm)


@pytest.mark.skipif(not LINUX, reason='needs /proc to identify the grandchild')
def test_uvloop_spawn_identity_and_group_stop(store_env, tmp_path):
    """Production's event loop (uvloop via uvicorn --loop auto): own session, verified listener, group stop."""
    uvloop = pytest.importorskip('uvloop')
    peers = Peers(tmp_path)
    port = free_port()
    args, ready, _log = peers.args(port, 'uvloop-synthetic', forker=True)

    async def scenario():
        await store_env.put('mcp_server', {'command': sys.executable, 'args': args, 'port': port, 'env': {}})
        pm = McpProcessManager()
        pm.auto_restart_delays = (30,)
        try:
            assert await pm.start()
            await asyncio.wait_for(pm._configuration_task, 15)
            leader, grandchild = pm._process.pid, int(ready.read_text())
            peers.track(grandchild)
            stat = Path(f'/proc/{grandchild}/stat').read_text().rpartition(')')[2].split()
            assert int(stat[2]) == leader and int(stat[3]) == leader  # pgrp and session of the grandchild
            assert pm.forward_generation(port) is not None and pm._applied_revision == pm._attempted_revision
            await pm.stop()
            assert await wait_until(lambda: not alive(grandchild), 5) and await wait_until(lambda: port_free(port), 5)
            assert pm.forward_generation(port) is None
        finally:
            await pm.stop()

    try:
        uvloop.run(scenario())
    finally:
        peers.cleanup()
