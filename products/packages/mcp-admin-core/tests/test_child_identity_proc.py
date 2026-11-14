"""``child_identity`` 的 OS 層判斷（/proc 解析、試綁、listener 擁有者、exec 包裝），以本程序的真 socket 驗證。"""
import ipaddress
import os
import socket
import subprocess
import sys

import pytest

from mcp_admin_core import child_identity
from mcp_admin_core.child_identity import Peer

LINUX = sys.platform.startswith('linux')
linux_only = pytest.mark.skipif(not LINUX, reason='needs /proc')


@pytest.mark.parametrize('text,expected', [
    ('0100007F', '127.0.0.1'),
    ('00000000', '0.0.0.0'),
    ('00000000000000000000000000000000', '::'),
    ('0000000000000000FFFF00000100007F', '::ffff:127.0.0.1'),
    ('00000000000000000000000001000000', '::1'),
])
def test_proc_net_addresses_are_decoded_per_32_bit_word(text, expected):
    if sys.byteorder != 'little':
        pytest.skip('fixtures are little-endian /proc output')
    assert child_identity._address(text) == ipaddress.ip_address(expected)


def test_only_addresses_that_accept_loopback_v4_connections_count():
    # 比 ipaddress 物件、不比 str()：IPv4-mapped IPv6 的文字形式隨 CPython 修補版本而變
    # （3.12.3 印 ::ffff:7f00:1，3.12.14 印 ::ffff:127.0.0.1），物件相等與版本無關。
    accepts = set(child_identity._ACCEPTS_LOOPBACK)
    assert accepts == {ipaddress.ip_address(text)
                       for text in ('127.0.0.1', '0.0.0.0', '::', '::ffff:127.0.0.1', '::ffff:0.0.0.0')}
    assert ipaddress.ip_address('127.0.0.2') not in child_identity._ACCEPTS_LOOPBACK
    assert ipaddress.ip_address('::1') not in child_identity._ACCEPTS_LOOPBACK


@linux_only
def test_listener_report_owned_foreign_and_not_listening():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        assert child_identity.listener_report(port, os.getsid(0)).verdict is Peer.NOT_LISTENING
        listener.listen()
        owned = child_identity.listener_report(port, os.getsid(0))
        assert owned.verdict is Peer.OWNED and owned.listeners == 1
        foreign = child_identity.listener_report(port, os.getsid(0) + 1)  # not this process's session
        assert foreign.verdict is Peer.FOREIGN and foreign.foreign_pids == (os.getpid(),)


@linux_only
def test_wildcard_listener_counts_and_other_loopback_address_does_not():
    with socket.socket() as wildcard:
        wildcard.bind(('0.0.0.0', 0))
        wildcard.listen()
        port = wildcard.getsockname()[1]
        assert child_identity.listener_report(port, os.getsid(0) + 1).verdict is Peer.FOREIGN
    with socket.socket() as other:
        try:
            other.bind(('127.0.0.2', 0))
        except OSError:
            pytest.skip('127.0.0.2 not routable here')
        other.listen()
        port = other.getsockname()[1]
        assert child_identity.listener_report(port, os.getsid(0) + 1).verdict is Peer.NOT_LISTENING


def test_port_occupied_detects_listeners_and_ignores_free_ports():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert child_identity.port_occupied(port)
    assert child_identity.port_occupied(port) is None


def test_port_occupied_detects_a_bound_socket_without_reuse():
    with socket.socket() as bound:
        bound.bind(('127.0.0.1', 0))  # bound, not listening, no SO_REUSEADDR
        assert child_identity.port_occupied(bound.getsockname()[1])


def test_port_occupied_detects_ipv6_loopback_listener():
    if not socket.has_ipv6:
        pytest.skip('no IPv6')
    with socket.socket(socket.AF_INET6) as listener:
        try:
            listener.bind(('::1', 0))
        except OSError:
            pytest.skip('::1 not available')
        listener.listen()
        assert child_identity.port_occupied(listener.getsockname()[1])


@linux_only
def test_admin_listener_ports_lists_this_process_listeners():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        assert listener.getsockname()[1] in child_identity.admin_listener_ports()


@pytest.mark.parametrize('argv,environ,expected', [
    (['/srv/venv/bin/uvicorn', 'app:app', '--host', '0.0.0.0', '--port', '8080'], {}, 8080),
    (['uvicorn', 'app:app', '--port=9000'], {}, 9000),
    (['uvicorn', 'app:app'], {'UVICORN_PORT': '7000'}, 7000),
    (['uvicorn', 'app:app'], {}, 8000),
    (['/usr/lib/python3/site-packages/uvicorn/__main__.py', 'app:app', '--port', '8081'], {}, 8081),
    (['uvicorn', 'app:app', '--fd', '3'], {}, None),
    (['uvicorn', 'app:app', '--uds', '/tmp/x.sock'], {}, None),
    (['/usr/bin/pytest', '--port', '8080'], {}, None),
    ([], {}, None),
])
def test_uvicorn_command_line_port(argv, environ, expected):
    assert child_identity._uvicorn_cli_port(argv, environ) == expected


@linux_only
def test_spawn_argv_sets_parent_death_signal_in_the_real_command():
    code = ('import ctypes; libc = ctypes.CDLL(None); value = ctypes.c_int(); '
            'libc.prctl(2, ctypes.byref(value), 0, 0, 0); print(value.value)')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8'}
    argv, parent_death = child_identity.spawn_argv(sys.executable, ['-I', '-S', '-c', code], env)
    assert parent_death and argv[:4] == [sys.executable, '-I', '-S', '-c']
    assert argv[-5:] == [sys.executable, '-I', '-S', '-c', code]
    out = subprocess.run(argv, env=env, capture_output=True, text=True, check=True, start_new_session=True).stdout
    assert out.strip() == '9'  # PR_GET_PDEATHSIG after exec: SIGKILL


@linux_only
@pytest.mark.parametrize('extra', [{}, {'LC_CTYPE': 'C'}, {'LC_CTYPE': ''}, {'LANG': 'C', 'LC_ALL': 'C'}])
def test_spawn_argv_hands_the_exact_env_to_the_real_command(extra):
    """PEP 538 coerces LC_CTYPE in the wrapper interpreter; the real command still sees the original env."""
    env = {'PATH': '/usr/bin:/bin', 'SYNTHETIC_ONLY': 'a=b', **extra}
    argv, parent_death = child_identity.spawn_argv('env', ['-0'], env)
    assert parent_death
    out = subprocess.run(argv, env=env, capture_output=True, check=True).stdout
    assert dict(item.split('=', 1) for item in out.decode().split('\0') if item) == env


@linux_only
def test_spawn_argv_keeps_the_signal_dispositions_popen_gives_a_direct_child():
    """The wrapper interpreter ignores SIGPIPE/SIGXFSZ; the real command must not inherit that (restore_signals)."""
    env = {'PATH': '/usr/bin:/bin'}
    script = ['-c', 'grep -E "^Sig(Ign|Blk):" /proc/self/status']
    argv, parent_death = child_identity.spawn_argv('sh', script, env)
    assert parent_death
    wrapped = subprocess.run(argv, env=env, capture_output=True, text=True, check=True).stdout
    direct = subprocess.run(['sh', *script], env=env, capture_output=True, text=True, check=True).stdout
    assert wrapped == direct


def test_spawn_argv_leaves_an_unresolvable_command_to_fail_as_before():
    argv, parent_death = child_identity.spawn_argv('synthetic-command-that-does-not-exist', ['--x'], {'PATH': '/nonexistent'})
    assert argv == ['synthetic-command-that-does-not-exist', '--x'] and not parent_death


@linux_only
def test_exec_wrapper_refuses_when_the_expected_parent_is_gone():
    argv, _ = child_identity.spawn_argv('/bin/true', [], {'PATH': '/usr/bin:/bin'})
    argv[argv.index(str(os.getpid()))] = '1'  # pretend the manager was another process that already died
    done = subprocess.run(argv, env={'PATH': '/usr/bin:/bin'}, capture_output=True)
    assert done.returncode == 125 and done.stdout == b''


@linux_only
def test_sweep_group_reports_live_members_without_signalling():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        sweep = child_identity.sweep_group(child.pid, child.pid, None)
        assert [member.pid for member in sweep.live] == [child.pid] and not sweep.signalled
    finally:
        child.kill()
        child.wait(timeout=5)
    assert child_identity.sweep_group(child.pid, child.pid, None).live == []
