"""子行程晚於探測期限才開始 listen（P2 life-1）：探測逾時後改在背景、以同一套身分檢查低頻繼續驗證。

合成程序、假 token、只綁 127.0.0.1 的臨時 port。驗證規則與時間內成功的路徑完全相同（/proc 擁有者、session、
世代閘門）；不放寬任何身分保證。
"""
import asyncio
import sys

import pytest

from mcp_admin_core import child_identity
from test_child_identity_live import (  # noqa: F401  (peers is a fixture)
    INIT, LIST, LINUX, free_port, gateway, last_error, peers, post, stop, wait_until)

pytestmark = pytest.mark.skipif(not LINUX, reason='needs /proc to see who owns the listener')

# Waits for a gate file (argv[1]) before running the synthetic server (argv[2:]).
GATED = ("import os,sys,time,runpy\n"
         "while not os.path.exists(sys.argv[1]): time.sleep(0.05)\n"
         "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0], run_name='__main__')")


def fast(pm, timeout=0.5):
    pm.configuration_probe_timeout = timeout
    pm.late_verify_max_delay = 0.4


async def late_waiting(pm):
    """The probe window is over and the background check is the only thing left."""
    return await wait_until(lambda: pm.late_verification_pending and pm._configuration_task is None, 10)


async def test_child_listening_after_probe_timeout_becomes_forwardable(make_app, monkeypatch, peers, tmp_path):
    port = free_port()
    gate = tmp_path / 'gate'
    args, ready, _log = peers.args(port, 'late-synthetic')
    app, pm = gateway(make_app, monkeypatch, port, ['-c', GATED, str(gate), *args])
    fast(pm)
    try:
        assert await pm.start()
        assert await late_waiting(pm)
        # Waiting is visible (late_verification) and it is not applied; nothing is forwarded yet.
        status = await last_error(pm)
        assert status['apply_state'] == 'failed' and status['late_verification'] is True
        assert status['last_error_code'] == 'child_probe_failed' and status['applied_revision'] is None
        assert pm.forward_generation(port) is None
        assert (await post(app, INIT)).status_code == 503
        gate.write_text('go')
        assert await wait_until(lambda: pm._applied_revision is not None and pm._applied_revision == pm._attempted_revision, 15)
        response = await post(app, INIT)
        assert response.status_code == 200
        assert response.json()['result']['serverInfo']['name'] == f'late-synthetic-{pm._process.pid}'
        assert pm.forward_generation(port) is not None
        status = await last_error(pm)
        assert status['apply_state'] == 'applied' and status['last_error_code'] is None
        assert status['late_verification'] is False
        assert pm._late_verify_task is None and not pm.late_verification_pending
        assert 'setup_status' not in (await post(app, LIST)).text
    finally:
        await stop(pm)


async def test_child_that_never_listens_is_not_forwarded_does_not_spin_and_stop_cancels(make_app, monkeypatch, peers):
    port = free_port()
    app, pm = gateway(make_app, monkeypatch, port, ['-c', 'import time; time.sleep(3600)'])
    fast(pm, 0.3)
    calls = []
    real = child_identity.listener_report
    monkeypatch.setattr(child_identity, 'listener_report', lambda *a, **k: calls.append(1) or real(*a, **k))
    try:
        assert await pm.start()
        assert await late_waiting(pm)
        task = pm._late_verify_task
        before = len(calls)
        await asyncio.sleep(2.0)
        assert len(calls) - before <= 8, 'backoff: a handful of checks, not a busy loop'
        assert pm.forward_generation(port) is None and not task.done()
        assert (await post(app, INIT)).status_code == 503
        status = await last_error(pm)
        assert status['apply_state'] == 'failed' and status['late_verification'] is True
    finally:
        await stop(pm)
    assert task.done() and pm._late_verify_task is None
    status = await last_error(pm)
    assert status['late_verification'] is False and status['apply_state'] != 'applied'
    stopped = len(calls)
    await asyncio.sleep(0.6)
    assert len(calls) == stopped, 'no check after stop'


async def test_restart_while_waiting_cancels_old_background_check(make_app, monkeypatch, peers, tmp_path):
    port = free_port()
    gate = tmp_path / 'gate'
    args, ready, _log = peers.args(port, 'regen-synthetic')
    app, pm = gateway(make_app, monkeypatch, port, ['-c', GATED, str(gate), *args])
    fast(pm)
    try:
        assert await pm.start()
        assert await late_waiting(pm)
        old_task, old_pid = pm._late_verify_task, pm._process.pid
        assert await pm.restart()
        assert old_task.done(), 'old generation background check cancelled'
        assert pm._process.pid != old_pid
        assert await late_waiting(pm)
        new_task = pm._late_verify_task
        assert new_task is not old_task
        gate.write_text('go')
        assert await wait_until(lambda: pm._applied_revision is not None and pm._applied_revision == pm._attempted_revision, 15)
        response = await post(app, INIT)
        assert response.status_code == 200
        assert response.json()['result']['serverInfo']['name'] == f'regen-synthetic-{pm._process.pid}'
    finally:
        await stop(pm)


async def test_foreign_listener_after_probe_timeout_is_refused_as_peer_mismatch(make_app, monkeypatch, peers):
    port = free_port()
    app, pm = gateway(make_app, monkeypatch, port, ['-c', 'import time; time.sleep(3600)'])
    fast(pm, 0.3)
    try:
        assert await pm.start()
        child = pm._process
        assert await late_waiting(pm)
        rival_log = await peers.rival(port)
        assert await wait_until(lambda: child.returncode is not None, 15)  # the refused generation is stopped
        status = await last_error(pm)
        assert status['apply_state'] == 'failed' and status['last_error_code'] == 'child_peer_mismatch'
        assert status['late_verification'] is False and pm._late_verify_task is None
        assert pm.forward_generation(port) is None
        for message in (INIT, LIST):
            response = await post(app, message)
            assert 'rival-synthetic' not in response.text
        assert not rival_log.exists() or rival_log.read_text() == ''
    finally:
        await stop(pm)


async def test_child_exit_while_waiting_ends_background_check(make_app, monkeypatch, peers):
    port = free_port()
    app, pm = gateway(make_app, monkeypatch, port, ['-c', 'import time; time.sleep(1.2)'])
    fast(pm, 0.3)
    try:
        assert await pm.start()
        task = None
        assert await late_waiting(pm)
        task = pm._late_verify_task
        assert await wait_until(task.done, 10)
        assert pm._late_verify_task is None
        status = await last_error(pm)
        assert status['last_error_code'] == 'child_exited' and status['late_verification'] is False
    finally:
        await stop(pm)
