"""Offline restore publishes only fully revoked config trees."""
import json
import os
import subprocess
import sys

import pytest

PROFILE = '1234567890abcdef12345678'


@pytest.fixture(autouse=True)
def n8n_runtime_fixture(tmp_path, monkeypatch):
    package = tmp_path / 'n8n-package'
    (package / 'dist/mcp').mkdir(parents=True)
    (package / 'dist/mcp/index.js').touch()
    (package / 'package.json').write_text('{"name":"n8n-mcp","version":"2.96.2"}')
    monkeypatch.setenv('N8N_MCP_PACKAGE_DIR', str(package))


def seed(path):
    path.mkdir()
    other = path / 'profiles' / PROFILE
    other.mkdir(parents=True)
    common = {'admin_password': 'fixture-password', 'admin_session_generation': 'fixture-session',
              'mcp_auth_revoked': False, 'mcp_auth_generation': 'fixture-generation',
              'connection': {}, 'tools': {'disabled': [], 'disabled_operations': {},
                                         'owner_defaults_20261025': {'applied': 'fresh_install'}},
              'token_history': [], 'proxy': {}}
    default = {**common, 'mcp_auth_token': 'fixture-token-a', 'mcp_server': {'port': 18001},
               'profile_index': {PROFILE: {'label': 'B', 'port': 18002}}}
    child = {**common, 'mcp_auth_token': 'fixture-token-b', 'mcp_server': {'port': 18002}}
    for file, value in ((path / 'config.json', default), (other / 'config.json', child)):
        file.write_text(json.dumps(value))
        file.chmod(0o600)


def cli(*args):
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return subprocess.run([sys.executable, '-m', 'mcp_admin_core.restore_guard', *map(str, args)],
                          env=env, capture_output=True, text=True, timeout=15, check=False)


def test_restore_publishes_only_revoked_profiles_and_new_admin_generation(tmp_path):
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    result = cli('prepare', source, dest)
    assert result.returncode == 0
    configs = [json.loads((dest / p).read_text()) for p in ('config.json', f'profiles/{PROFILE}/config.json')]
    for config in configs:
        assert config['mcp_auth_token'] == ''
        assert config['mcp_auth_revoked'] is True
        assert config['mcp_auth_generation'] != 'fixture-generation'
        assert config['admin_session_generation'] != 'fixture-session'
        assert config['admin_password'] != 'fixture-password'
    assert json.loads((source / 'config.json').read_text())['mcp_auth_token'] == 'fixture-token-a'
    assert 'fixture-token' not in result.stdout + result.stderr


def test_missing_profile_fails_without_publishing_or_starting(tmp_path):
    source, dest, started = tmp_path / 'backup', tmp_path / 'ready', tmp_path / 'started'
    seed(source)
    (source / 'profiles' / PROFILE / 'config.json').unlink()
    result = cli('serve', source, dest, '--', sys.executable, '-c',
                 'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', started)
    assert result.returncode != 0
    assert not dest.exists()
    assert not started.exists()
    assert 'fixture-token' not in result.stdout + result.stderr


def test_existing_destination_is_never_replaced(tmp_path):
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    dest.mkdir()
    (dest / 'keep').write_text('unchanged')
    result = cli('prepare', source, dest)
    assert result.returncode != 0
    assert (dest / 'keep').read_text() == 'unchanged'


def test_unhandled_child_bearer_is_rejected_in_every_profile(tmp_path):
    for case in ('default-proxy', 'child-env'):
        case_dir = tmp_path / case
        case_dir.mkdir()
        source, dest = case_dir / 'backup', case_dir / 'ready'
        seed(source)
        path = source / ('config.json' if case == 'default-proxy' else f'profiles/{PROFILE}/config.json')
        config = json.loads(path.read_text())
        if case == 'default-proxy':
            config['proxy']['bearer_token'] = 'fixture-child-bearer'
        else:
            config['mcp_server']['env'] = {'AUTH_TOKEN': 'fixture-child-bearer'}
        path.write_text(json.dumps(config))
        result = cli('prepare', source, dest)
        assert result.returncode != 0
        assert not dest.exists()
        assert 'fixture-child-bearer' not in result.stdout + result.stderr


def test_index_config_mismatch_unindexed_profile_and_symlink_fail_closed(tmp_path):
    for case in ('mismatch', 'unindexed', 'symlink', 'duplicate'):
        case_dir = tmp_path / case
        case_dir.mkdir()
        source, dest = case_dir / 'backup', case_dir / 'ready'
        seed(source)
        config = source / 'profiles' / PROFILE / 'config.json'
        if case == 'mismatch':
            value = json.loads(config.read_text())
            value['mcp_server']['port'] = 18003
            config.write_text(json.dumps(value))
        elif case == 'unindexed':
            (source / 'profiles' / ('f' * 24)).mkdir()
        elif case == 'symlink':
            config.unlink()
            config.symlink_to(source / 'config.json')
        else:
            config.write_text('{"mcp_auth_token":"first","mcp_auth_token":"second"}')
        result = cli('prepare', source, dest)
        assert result.returncode != 0, case
        assert not dest.exists(), case
        assert 'fixture-token' not in result.stdout + result.stderr


def test_partial_write_and_abrupt_interruption_do_not_publish_or_start(tmp_path):
    source = tmp_path / 'backup'
    seed(source)
    script = '''
import os, sys
from mcp_admin_core import restore_guard as g
real = g._write_config
count = 0
def fail(path, config):
    global count
    count += 1
    if count == 2:
        if sys.argv[1] == 'crash': os._exit(57)
        raise OSError('private details must not be printed')
    real(path, config)
g._write_config = fail
sys.exit(g.main(['serve', sys.argv[2], sys.argv[3], '--', sys.executable,
                 '-c', 'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', sys.argv[4]]))
'''
    for mode in ('error', 'crash'):
        dest, marker = tmp_path / ('ready-' + mode), tmp_path / ('started-' + mode)
        result = subprocess.run([sys.executable, '-c', script, mode, str(source), str(dest), str(marker)],
                                capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode != 0
        assert not dest.exists()
        assert not marker.exists()
        assert 'fixture-token' not in result.stdout + result.stderr
        assert 'private details' not in result.stderr
    orphaned = list(tmp_path.glob('.mcp-restore-*'))
    assert len(orphaned) == 1
    assert orphaned[0].stat().st_mode & 0o777 == 0o700
    partial = json.loads((orphaned[0] / 'config.json').read_text())
    assert partial['mcp_auth_token'] == '' and partial['mcp_auth_revoked'] is True
    # A subsequent fresh attempt is independent of any orphaned private stage.
    assert cli('prepare', source, tmp_path / 'retry').returncode == 0


def test_concurrent_destination_creation_cannot_be_overwritten(tmp_path):
    source, dest, marker = tmp_path / 'backup', tmp_path / 'ready', tmp_path / 'started'
    seed(source)
    script = '''
import sys
from pathlib import Path
from mcp_admin_core import restore_guard as g
real = g._publish
def racing(source, destination):
    destination.mkdir()
    (destination / 'sentinel').write_text('untouched')
    real(source, destination)
g._publish = racing
sys.exit(g.main(['serve', sys.argv[1], sys.argv[2], '--', sys.executable,
                 '-c', 'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', sys.argv[3]]))
'''
    result = subprocess.run([sys.executable, '-c', script, str(source), str(dest), str(marker)],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode != 0
    assert (dest / 'sentinel').read_text() == 'untouched'
    assert not marker.exists()


def test_post_publish_fsync_failure_never_starts_service(tmp_path):
    source, dest, marker = tmp_path / 'backup', tmp_path / 'ready', tmp_path / 'started'
    seed(source)
    script = '''
import sys
from mcp_admin_core import restore_guard as g
real = g._fsync_dir
def fail_parent(path):
    if path == __import__('pathlib').Path(sys.argv[2]).parent: raise OSError('sensitive detail')
    real(path)
g._fsync_dir = fail_parent
sys.exit(g.main(['serve', sys.argv[1], sys.argv[2], '--', sys.executable,
                 '-c', 'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', sys.argv[3]]))
'''
    result = subprocess.run([sys.executable, '-c', script, str(source), str(dest), str(marker)],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode != 0
    assert not marker.exists()
    assert 'sensitive detail' not in result.stderr
    for path in (dest / 'config.json', dest / 'profiles' / PROFILE / 'config.json'):
        config = json.loads(path.read_text())
        assert config['mcp_auth_revoked'] is True and config['mcp_auth_token'] == ''


def seed_n8n(source):
    import sqlite3
    from pathlib import Path

    schema = (Path(__file__).parent / 'fixtures' / 'n8n_catalog_schema.sql').read_text()
    seed(source)
    paths = [source, source / 'profiles' / PROFILE]
    old = ['fixture-old-internal-a', 'fixture-old-internal-b']
    for i, directory in enumerate(paths):
        config_path = directory / 'config.json'
        cfg = json.loads(config_path.read_text())
        cfg['mcp_server']['env'] = {'AUTH_TOKEN': old[i]}
        cfg['proxy']['bearer_token'] = old[i]
        config_path.write_text(json.dumps(cfg))
        db_dir = directory / 'n8n-mcp'
        db_dir.mkdir()
        with sqlite3.connect(db_dir / 'nodes.db') as db:
            db.executescript(schema)
            db.execute('insert into nodes(node_type,package_name,display_name) values(?,?,?)',
                       ('n8n-synthetic-' + str(i), 'synthetic', 'Synthetic'))
            db.execute('insert into nodes_fts(rowid,node_type,display_name) '
                       'select rowid,node_type,display_name from nodes')
            db.execute('create table workflow_history(id integer primary key, marker text)')
            db.execute('insert into workflow_history(marker) values(?)', ('history-' + str(i),))
        (db_dir / '.seed.lock').touch()
    return old


def test_n8n_restore_rotates_each_internal_pair_and_preserves_history(tmp_path):
    import sqlite3
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    old = seed_n8n(source)
    result = cli('--product', 'n8n', 'prepare', source, dest)
    assert result.returncode == 0
    fresh = []
    for i, directory in enumerate((dest, dest / 'profiles' / PROFILE)):
        config = json.loads((directory / 'config.json').read_text())
        value = config['mcp_server']['env']['AUTH_TOKEN']
        valid_pair = config['proxy']['bearer_token'] == value and bool(value) and value not in old
        assert valid_pair
        assert config['mcp_auth_revoked'] is True and config['mcp_auth_token'] == ''
        fresh.append(value)
        with sqlite3.connect(directory / 'n8n-mcp' / 'nodes.db') as db:
            assert db.execute('select marker from workflow_history').fetchone() == ('history-' + str(i),)
    distinct = fresh[0] != fresh[1]
    assert distinct
    assert all(s not in result.stdout + result.stderr for s in old + fresh)


def test_n8n_incomplete_or_unsafe_backup_never_starts(tmp_path):
    for case in ('mismatch', 'missing-bearer', 'missing-db', 'wal', 'shm', 'corrupt-db', 'symlink-db', 'extra', 'unknown-journal-link'):
        case_dir = tmp_path / case
        case_dir.mkdir()
        source, dest, marker = case_dir / 'backup', case_dir / 'ready', case_dir / 'started'
        old = seed_n8n(source)
        child = source / 'profiles' / PROFILE
        config_path = child / 'config.json'
        config = json.loads(config_path.read_text())
        db = child / 'n8n-mcp' / 'nodes.db'
        if case == 'mismatch':
            config['proxy']['bearer_token'] = 'wrong-bearer'
            config_path.write_text(json.dumps(config))
        elif case == 'missing-bearer':
            del config['mcp_server']['env']['AUTH_TOKEN']
            config_path.write_text(json.dumps(config))
        elif case == 'missing-db':
            db.unlink()
        elif case in ('wal', 'shm'):
            (db.parent / ('nodes.db-' + case)).touch()
        elif case == 'corrupt-db':
            db.write_bytes(b'invalid sqlite')
        elif case == 'symlink-db':
            db.unlink()
            db.symlink_to(source / 'n8n-mcp' / 'nodes.db')
        elif case == 'extra':
            (child / 'unexpected').touch()
        else:
            (child / '.config.json.operations.json').symlink_to(config_path)
        before = (source / 'config.json').read_bytes()
        result = cli('--product', 'n8n', 'serve', source, dest, '--', sys.executable, '-c',
                     'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', marker)
        assert result.returncode != 0, case
        assert not dest.exists(), case
        assert not marker.exists(), case
        assert (source / 'config.json').read_bytes() == before
        assert all(token not in result.stdout + result.stderr for token in old)


def test_n8n_partial_database_copy_and_crash_do_not_publish(tmp_path):
    source = tmp_path / 'backup'
    seed_n8n(source)
    script = '''
import os, sys
from mcp_admin_core import restore_guard as g
real = g._copy_n8n_database
count = 0
def fail(source, destination):
    global count
    count += 1
    if count == 2:
        if sys.argv[1] == 'crash': os._exit(57)
        raise OSError('private failure')
    real(source, destination)
g._copy_n8n_database = fail
sys.exit(g.main(['--product', 'n8n', 'serve', sys.argv[2], sys.argv[3], '--', sys.executable,
                 '-c', 'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', sys.argv[4]]))
'''
    for mode in ('error', 'crash'):
        dest, marker = tmp_path / ('ready-' + mode), tmp_path / ('started-' + mode)
        result = subprocess.run([sys.executable, '-c', script, mode, str(source), str(dest), str(marker)],
                                capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode != 0
        assert not dest.exists()
        assert not marker.exists()
        assert 'private failure' not in result.stderr
    assert cli('--product', 'n8n', 'prepare', source, tmp_path / 'retry').returncode == 0


def test_missing_policy_components_never_publish(tmp_path):
    for case in ('tools', 'disabled', 'disabled_operations'):
        case_dir = tmp_path / case
        case_dir.mkdir()
        source, dest = case_dir / 'backup', case_dir / 'ready'
        seed(source)
        path = source / 'profiles' / PROFILE / 'config.json'
        config = json.loads(path.read_text())
        if case == 'tools':
            del config['tools']
        else:
            del config['tools'][case]
        path.write_text(json.dumps(config))
        result = cli('prepare', source, dest)
        assert result.returncode != 0 and not dest.exists()


def test_deeply_nested_input_fails_without_traceback_or_secret(tmp_path):
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    (source / 'config.json').write_text('{"secret":"fixture-secret","x":' + '[' * 1500 + '0' + ']' * 1500 + '}')
    result = cli('prepare', source, dest)
    assert result.returncode != 0 and not dest.exists()
    assert 'Traceback' not in result.stderr and 'fixture-secret' not in result.stderr


def test_untrusted_configuration_keys_are_not_logged(tmp_path, caplog):
    from mcp_admin_core.process import _build_subprocess_env
    from odoo_mcp_admin.main import LAUNCH_POLICY
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    path = source / 'config.json'
    config = json.loads(path.read_text())
    config['connection']['fixture-secret-as-field-name'] = 'x'
    path.write_text(json.dumps(config))
    assert cli('prepare', source, dest).returncode == 0
    restored = json.loads((dest / 'config.json').read_text())
    _build_subprocess_env(restored['connection'], restored['mcp_server'], LAUNCH_POLICY, restored)
    assert 'fixture-secret-as-field-name' not in caplog.text


@pytest.mark.parametrize('product', ['generic', 'n8n'])
def test_journal_is_archived_and_old_operation_id_cannot_replay(tmp_path, product):
    import time

    from fastapi import HTTPException
    from mcp_admin_core.operations import OperationJournal
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    (seed_n8n if product == 'n8n' else seed)(source)
    old_id = f'{int(time.time()*1000)-5000}.' + 'a' * 32
    original_bytes = []
    for directory in (source, source / 'profiles' / PROFILE):
        journal = OperationJournal(directory / 'config.json')
        value = {'version': 1, 'key': 'a' * 64, 'expired_before_ms': 0,
                 'receipts': {old_id: {'kind': 'token.rotate', 'state': 'completed',
                                       'fingerprint': 'b' * 64, 'result': {'status': 'ok'}}}}
        journal._write(value)
        original_bytes.append(journal.path.read_bytes())
    assert cli('--product', product, 'prepare', source, dest).returncode == 0
    for i, directory in enumerate((dest, dest / 'profiles' / PROFILE)):
        archives = list((directory / 'restore-operation-journals').iterdir())
        assert len(archives) == 1
        assert archives[0].read_bytes() == original_bytes[i]
        assert archives[0].stat().st_mode & 0o777 == 0o600
        journal = OperationJournal(directory / 'config.json')
        with pytest.raises(HTTPException) as rejected:
            journal.query(old_id)
        assert rejected.value.status_code == 410
    # A later restore retains the earlier archive and archives the active journal.
    second = tmp_path / 'second'
    assert cli('--product', product, 'prepare', dest, second).returncode == 0
    for i, directory in enumerate((second, second / 'profiles' / PROFILE)):
        archives = list((directory / 'restore-operation-journals').iterdir())
        assert len(archives) == 2
        assert original_bytes[i] in [path.read_bytes() for path in archives]


def test_n8n_bad_journal_is_not_discarded_or_published(tmp_path):
    source, dest, marker = tmp_path / 'backup', tmp_path / 'ready', tmp_path / 'started'
    seed_n8n(source)
    (source / 'profiles' / PROFILE / '.config.json.operations.json').write_text('{"broken":true}')
    result = cli('--product', 'n8n', 'serve', source, dest, '--', sys.executable, '-c',
                 'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', marker)
    assert result.returncode != 0
    assert not dest.exists() and not marker.exists()
    assert 'Traceback' not in result.stderr


def test_n8n_incomplete_catalog_schema_cannot_be_published(tmp_path):
    import sqlite3

    source, dest, started = tmp_path / 'backup', tmp_path / 'ready', tmp_path / 'started'
    seed_n8n(source)
    database = source / 'profiles' / PROFILE / 'n8n-mcp' / 'nodes.db'
    database.unlink()
    with sqlite3.connect(database) as db:
        db.execute('create table nodes(id integer)')
        db.execute('insert into nodes values (1)')
    result = cli('--product', 'n8n', 'serve', source, dest, '--', sys.executable, '-c',
                 'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', started)
    assert result.returncode != 0
    assert not dest.exists() and not started.exists()


def test_n8n_wrong_runtime_or_broken_search_index_cannot_be_published(tmp_path):
    import sqlite3

    source = tmp_path / 'backup'
    seed_n8n(source)
    package = tmp_path / 'n8n-package' / 'package.json'
    package.write_text('{"name":"n8n-mcp","version":"99.0.0"}')
    dest = tmp_path / 'wrong-version'
    assert cli('--product', 'n8n', 'prepare', source, dest).returncode != 0
    assert not dest.exists()
    package.write_text('{"name":"n8n-mcp","version":"2.96.2"}')
    database = source / 'profiles' / PROFILE / 'n8n-mcp' / 'nodes.db'
    with sqlite3.connect(database) as db:
        db.execute('delete from nodes_fts')
    dest = tmp_path / 'broken-index'
    assert cli('--product', 'n8n', 'prepare', source, dest).returncode != 0
    assert not dest.exists()


def test_journal_capacity_compacts_without_losing_bytes_and_can_restore_again(tmp_path):
    import zipfile

    from mcp_admin_core.operations import OperationJournal

    source, first, second = tmp_path / 'backup', tmp_path / 'first', tmp_path / 'second'
    seed(source)
    archive = source / 'restore-operation-journals'
    archive.mkdir()
    original = {}
    for i in range(127):
        path = archive / (f'{i:032x}' + '.json')
        value = {'version': 1, 'key': f'{i:064x}', 'expired_before_ms': i, 'receipts': {}}
        path.write_text(json.dumps(value))
        original[path.name] = path.read_bytes()
    journal = OperationJournal(source / 'config.json')
    journal._write({'version': 1, 'key': 'a' * 64, 'expired_before_ms': 0, 'receipts': {}})
    original_active = journal.path.read_bytes()
    assert cli('prepare', source, first).returncode == 0
    assert cli('prepare', first, second).returncode == 0
    observed = []
    for path in (second / 'restore-operation-journals').iterdir():
        if path.suffix == '.zip':
            with zipfile.ZipFile(path) as bundle:
                observed.extend(bundle.read(name) for name in bundle.namelist())
        else:
            observed.append(path.read_bytes())
    assert all(value in observed for value in original.values())
    assert original_active in observed


def test_corrupt_journal_bundle_is_rejected_without_publication(tmp_path):
    import zipfile

    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    archive = source / 'restore-operation-journals'
    archive.mkdir()
    with zipfile.ZipFile(archive / ('d' * 32 + '.zip'), 'w') as bundle:
        bundle.writestr('e' * 32 + '.json', b'{"invalid":true}')
    assert cli('prepare', source, dest).returncode != 0
    assert not dest.exists()


@pytest.mark.parametrize('placement', ['active', 'archive', 'bundle'])
@pytest.mark.parametrize('duplicate', ['cutoff', 'receipt'])
def test_ambiguous_journal_is_rejected_in_either_profile(tmp_path, placement, duplicate):
    import zipfile

    source, dest, marker = tmp_path / 'backup', tmp_path / 'ready', tmp_path / 'started'
    seed(source)
    child = source / 'profiles' / PROFILE
    if duplicate == 'cutoff':
        raw = '{"version":1,"key":"' + 'a' * 64 + '","expired_before_ms":9000000000000,' \
              '"expired_before_ms":0,"receipts":{}}'
    else:
        op = '1800000000000.' + 'b' * 32
        raw = '{"version":1,"key":"' + 'a' * 64 + '","expired_before_ms":0,"receipts":{"' + op \
              + '":{"state":"pending","state":"completed","fingerprint":"' + 'b' * 64 \
              + '","kind":"token.rotate","result":{"status":"ok"}}}}'
    if placement == 'active':
        (source / '.config.json.operations.json').write_text(raw)
    else:
        archive = child / 'restore-operation-journals'
        archive.mkdir()
        if placement == 'archive':
            (archive / ('c' * 32 + '.json')).write_text(raw)
        else:
            with zipfile.ZipFile(archive / ('d' * 32 + '.zip'), 'w') as bundle:
                bundle.writestr('c' * 32 + '.json', raw)
    result = cli('serve', source, dest, '--', sys.executable, '-c',
                 'from pathlib import Path; Path(__import__("sys").argv[1]).touch()', marker)
    assert result.returncode != 0 and not dest.exists() and not marker.exists()
    assert 'a' * 64 not in result.stdout + result.stderr


@pytest.mark.parametrize('count', [127, 128])
def test_compaction_preserves_every_profile_across_repeated_restores(tmp_path, count):
    import zipfile
    from collections import Counter

    from mcp_admin_core.operations import OperationJournal

    source, first, second = tmp_path / 'backup', tmp_path / 'first', tmp_path / 'second'
    seed(source)
    originals = []
    for index, directory in enumerate((source, source / 'profiles' / PROFILE)):
        archive = directory / 'restore-operation-journals'
        archive.mkdir()
        raw = json.dumps({'version': 1, 'key': str(index + 1) * 64,
                          'expired_before_ms': 1800000000000 + index, 'receipts': {}}).encode()
        for i in range(count):
            (archive / (f'{i:032x}' + '.json')).write_bytes(raw)
        expected = [raw] * count
        if count == 127:
            with zipfile.ZipFile(archive / ('f' * 32 + '.zip'), 'w') as bundle:
                bundle.writestr('e' * 32 + '.json', raw)
            expected.append(raw)
        journal = OperationJournal(directory / 'config.json')
        journal._write({'version': 1, 'key': 'a' * 64, 'expired_before_ms': 0, 'receipts': {}})
        expected.append(journal.path.read_bytes())
        originals.append(Counter(expected))
    assert cli('prepare', source, first).returncode == 0
    assert cli('prepare', first, second).returncode == 0
    for index, directory in enumerate((second, second / 'profiles' / PROFILE)):
        actual = []
        for path in (directory / 'restore-operation-journals').iterdir():
            if path.suffix == '.zip':
                with zipfile.ZipFile(path) as bundle:
                    assert len(bundle.infolist()) <= 128
                    actual.extend(bundle.read(info) for info in bundle.infolist())
            else:
                actual.append(path.read_bytes())
        assert not originals[index] - Counter(actual)
        assert OperationJournal(directory / 'config.json')._read()['expired_before_ms'] >= 1800000000000 + index


def test_archive_bundle_capacity_failure_preserves_source(tmp_path):
    import zipfile

    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    archive = source / 'restore-operation-journals'
    archive.mkdir()
    raw = json.dumps({'version': 1, 'key': 'a' * 64, 'expired_before_ms': 0, 'receipts': {}})
    for i in range(128):
        with zipfile.ZipFile(archive / (f'{i:032x}' + '.zip'), 'w') as bundle:
            bundle.writestr('e' * 32 + '.json', raw)
    (source / '.config.json.operations.json').write_text(raw)
    before = {p.name: p.read_bytes() for p in archive.iterdir()}
    assert cli('prepare', source, dest).returncode != 0
    assert not dest.exists()
    assert before == {p.name: p.read_bytes() for p in archive.iterdir()}


@pytest.mark.parametrize('archived', [False, True])
def test_restore_never_decreases_journal_expiry_even_if_snapshot_clock_was_ahead(tmp_path, archived):
    import time

    from mcp_admin_core.operations import OperationJournal

    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    future = int(time.time() * 1000) + 300000
    old_id = f'{future}.' + 'a' * 32
    journal = OperationJournal(source / 'config.json')
    journal._write({'version': 1, 'key': 'a' * 64, 'expired_before_ms': future - 1000,
                    'receipts': {old_id: {'kind': 'token.rotate', 'state': 'pending',
                                          'fingerprint': 'b' * 64}}})
    if archived:
        archive_dir = source / 'restore-operation-journals'
        archive_dir.mkdir()
        journal.path.rename(archive_dir / ('c' * 32 + '.json'))
    assert cli('prepare', source, dest).returncode == 0
    restored = OperationJournal(dest / 'config.json')._read()
    assert restored['expired_before_ms'] >= future
    sibling = OperationJournal(dest / 'profiles' / PROFILE / 'config.json')._read()
    assert sibling['expired_before_ms'] < future


def test_unmarked_legacy_config_with_unknown_security_switches_is_rejected(tmp_path):
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    path = source / 'config.json'
    config = json.loads(path.read_text())
    del config['tools']['owner_defaults_20261025']
    config['mcp_server']['env'] = {'ODOO_MCP_ALLOW_DIRECT_WRITES': '0'}
    path.write_text(json.dumps(config))
    result = cli('prepare', source, dest)
    assert result.returncode != 0 and not dest.exists()


# F3 restore-1: platform reset-config leaves /data/.config.json.reset-backup-<uuid4().hex> (a copy of the previous
# config.json, including its old plaintext credentials). Exact-name regular files at the config root are skipped.
RESET_BACKUP = '.config.json.reset-backup-' + '0123456789abcdef' * 2


def test_reset_backup_regular_file_at_root_is_skipped_never_read_or_copied(tmp_path):
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    leftover = source / RESET_BACKUP
    original = b'{"admin_password": "stale-reset-backup-password", "mcp_auth_token": "stale-reset-token"'
    leftover.write_bytes(original)
    leftover.chmod(0o000)  # unreadable and not valid JSON: it must not even be opened
    before = leftover.read_bytes() if os.access(leftover, os.R_OK) else None
    result = cli('prepare', source, dest)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in dest.iterdir()) == ['.config.json.operations.json', 'config.json', 'profiles']
    for path in dest.rglob('*'):
        if path.is_file():
            assert b'stale-reset' not in path.read_bytes()
    assert 'stale-reset' not in result.stdout + result.stderr
    assert leftover.exists()  # source untouched
    if os.geteuid() != 0:
        # root 不受檔案權限限制（chmod 000 照樣讀得到），無法用「讀不到」證明「沒被打開」；只在非 root 檢查這一項
        # （CI 以 uid 1000 執行）。root 下其餘斷言（沒複製、沒印出）照舊。
        assert before is None
    leftover.chmod(0o600)
    assert leftover.read_bytes() == original  # 來源檔內容沒有被改動


@pytest.mark.parametrize('name', [
    '.config.json.reset-backup-' + '0123456789ABCDEF' * 2,         # upper-case hex
    '.config.json.reset-backup-' + '0123456789abcdef' * 2 + '0',   # 33 chars
    '.config.json.reset-backup-' + '0123456789abcdef' * 2 + '.json',
    '.config.json.reset-backup-' + '0123456789abcdef' * 2 + '\n',
    '.config.json.reset-backup-' + '0123456789abcdeg' * 2,         # non-hex
    '.config.json.reset-backup-' + '0123456789abcde' * 2,          # 30 chars
    '.config.json.reset-backup-',
    '.Config.json.reset-backup-' + '0123456789abcdef' * 2,
    'config.json.reset-backup-' + '0123456789abcdef' * 2,
])
def test_reset_backup_lookalike_names_are_still_rejected(tmp_path, name):
    source = tmp_path / 'backup'
    seed(source)
    (source / name).write_text('x')
    result = cli('prepare', source, tmp_path / 'ready')
    assert result.returncode == 1
    assert not (tmp_path / 'ready').exists()


@pytest.mark.parametrize('kind', ['symlink', 'dangling_symlink', 'directory', 'fifo'])
def test_reset_backup_exact_name_that_is_not_a_regular_file_is_rejected(tmp_path, kind):
    source = tmp_path / 'backup'
    seed(source)
    path = source / RESET_BACKUP
    if kind == 'symlink':
        path.symlink_to(source / 'config.json')
    elif kind == 'dangling_symlink':
        path.symlink_to(tmp_path / 'missing')
    elif kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    result = cli('prepare', source, tmp_path / 'ready')
    assert result.returncode == 1
    assert not (tmp_path / 'ready').exists()


def test_reset_backup_is_only_ignored_at_the_root_not_inside_a_profile(tmp_path):
    source = tmp_path / 'backup'
    seed(source)
    (source / 'profiles' / PROFILE / RESET_BACKUP).write_text('x')
    result = cli('prepare', source, tmp_path / 'ready')
    assert result.returncode == 1


# 2026-11-13 mp-gap-prod-restore P1: the Headscale admin process creates <config dir>/admin-sessions.sqlite3 on every
# start (per-login revocations + random epoch), so every Headscale backup has it. It is never restored — the service
# creates a new one with a new epoch — and neither are its SQLite companions. Exact names, root only, regular files.
ADMIN_SESSIONS = ['admin-sessions.sqlite3', 'admin-sessions.sqlite3-journal', 'admin-sessions.sqlite3-wal',
                  'admin-sessions.sqlite3-shm']


def test_admin_sessions_database_and_companions_at_root_are_skipped_never_read_or_copied(tmp_path):
    source, dest = tmp_path / 'backup', tmp_path / 'ready'
    seed(source)
    originals = {}
    for name in ADMIN_SESSIONS:
        path = source / name
        originals[name] = ('SQLite format 3\x00 stale-session-canary ' + name).encode()
        path.write_bytes(originals[name])
        path.chmod(0o000)  # unreadable: it must not even be opened
    result = cli('prepare', source, dest)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in dest.iterdir()) == ['.config.json.operations.json', 'config.json', 'profiles']
    for path in dest.rglob('*'):
        if path.is_file():
            assert b'stale-session-canary' not in path.read_bytes()
    assert 'stale-session-canary' not in result.stdout + result.stderr
    for name, original in originals.items():
        path = source / name
        if os.geteuid() != 0:  # root ignores file modes (see the reset-backup test); CI runs as uid 1000
            assert not os.access(path, os.R_OK)
        path.chmod(0o600)
        assert path.read_bytes() == original  # the snapshot is untouched


@pytest.mark.parametrize('name', [
    'admin-sessions.sqlite',
    'admin-sessions.sqlite3.bak',
    'admin-sessions.sqlite3-journal.1',
    'admin-sessions.sqlite3-wal2',
    'admin-sessions.sqlite3-mj0123abcd',   # a SQLite super-journal is not expected next to this database
    'admin-sessions.sqlite3 ',
    'admin-sessions.sqlite3\n',
    '.admin-sessions.sqlite3',
    'Admin-sessions.sqlite3',
    'admin-sessions.SQLITE3',
    'admin_sessions.sqlite3',
    'admin-sessions.db',
    'sessions.sqlite3',
])
def test_admin_sessions_lookalike_names_are_still_rejected(tmp_path, name):
    source = tmp_path / 'backup'
    seed(source)
    (source / 'admin-sessions.sqlite3').write_bytes(b'SQLite format 3\x00')
    (source / name).write_text('x')
    result = cli('prepare', source, tmp_path / 'ready')
    assert result.returncode == 1
    assert not (tmp_path / 'ready').exists()


@pytest.mark.parametrize('name', ADMIN_SESSIONS)
@pytest.mark.parametrize('kind', ['symlink', 'dangling_symlink', 'directory', 'fifo'])
def test_admin_sessions_exact_name_that_is_not_a_regular_file_is_rejected(tmp_path, name, kind):
    source = tmp_path / 'backup'
    seed(source)
    path = source / name
    if kind == 'symlink':
        path.symlink_to(source / 'config.json')
    elif kind == 'dangling_symlink':
        path.symlink_to(tmp_path / 'missing')
    elif kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    result = cli('prepare', source, tmp_path / 'ready')
    assert result.returncode == 1
    assert not (tmp_path / 'ready').exists()


@pytest.mark.parametrize('name', ADMIN_SESSIONS)
def test_admin_sessions_files_are_only_ignored_at_the_root_not_inside_a_profile(tmp_path, name):
    # The admin process keeps one database next to the root config.json; a profile directory never has one.
    source = tmp_path / 'backup'
    seed(source)
    (source / 'profiles' / PROFILE / name).write_bytes(b'SQLite format 3\x00')
    result = cli('prepare', source, tmp_path / 'ready')
    assert result.returncode == 1
    assert not (tmp_path / 'ready').exists()
