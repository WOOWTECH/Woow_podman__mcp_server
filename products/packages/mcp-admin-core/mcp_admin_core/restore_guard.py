"""Prepare a supported MCP backup offline; not wired into PaaS restore.

The caller MUST keep all existing replicas and public routes stopped until the
new process is ready. This command cannot enforce that cluster-level gate or
protect an out-of-band PVC rollback. Input must be a stable, private snapshot.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path

from .config.store import _DEFAULT_CONFIG, ConfigUnavailable
from .operation_policy import read_policy
from .operations import OperationJournal
from .owner_defaults import MARKER, RESTORE_POLICY_HOLD
from .profiles import _validated_index


# The platform's reset-config (paas-operator releases.py) writes the previous config.json to
# /data/.config.json.reset-backup-<uuid4().hex> before replacing it and its cleanup can fail, so the file stays
# on the volume and later backups contain it. It holds the OLD plaintext credentials: it is never read, copied,
# published or counted, exactly like lost+found. Only the exact name (lower-case hex, 32 chars) is covered.
_RESET_BACKUP = re.compile(r'\.config\.json\.reset-backup-[0-9a-f]{32}', re.ASCII)
# Headscale's admin process keeps per-login revocations and a random epoch in <config dir>/admin-sessions.sqlite3
# (apps/headscale/headscale_mcp_admin/admin_sessions.py) and creates it on every start, so every backup of a
# Headscale volume has it. It is never restored: the service creates a new database with a new random epoch on its
# next start, so no admin login from before the restore is accepted again, independently of the
# admin_session_generation and admin_password this restore replaces. Restoring it could only bring back the backup's
# epoch and lose logouts made after the backup. Its SQLite companions (a backup of a running service can catch the
# rollback journal; -wal/-shm in WAL mode) go with it: without the database they are meaningless, and a hot journal
# published next to the new database would be replayed into it. Exact names, config root only, regular files only.
_ADMIN_SESSIONS = frozenset('admin-sessions.sqlite3' + suffix for suffix in ('', '-journal', '-wal', '-shm'))


class RestoreError(Exception):
    """Restoration cannot be safely published."""


# Schema contract of the deployed n8n-mcp 2.96.2 catalog. Additional columns
# are allowed, but absent tables/columns must never be treated as a valid DB.
_N8N_TABLE_COLUMNS = {
    'nodes': 'node_type package_name display_name description category development_style is_ai_tool is_trigger '
             'is_webhook is_versioned version documentation properties_schema operations credentials_required '
             'outputs output_names updated_at is_tool_variant has_tool_variant base_node_type tool_variant_of '
             'is_community is_verified author_name author_github_url npm_package_name npm_version npm_downloads '
             'community_fetched_at npm_readme ai_documentation_summary ai_summary_generated_at deprecated_versions',
    'node_versions': 'id node_type version package_name display_name description category is_current_max '
                     'properties_schema operations credentials_required outputs minimum_n8n_version breaking_changes '
                     'deprecated_properties added_properties released_at created_at',
    'templates': 'id workflow_id name description author_name author_username author_verified nodes_used workflow_json '
                 'workflow_json_compressed categories views created_at updated_at url scraped_at metadata_json '
                 'metadata_generated_at',
    'template_node_configs': 'id node_type template_id template_name template_views node_name parameters_json '
                             'credentials_json has_credentials has_expressions complexity use_cases rank created_at',
    'workflow_versions': 'id instance_id workflow_id version_number workflow_name workflow_snapshot trigger '
                         'operations fix_types metadata created_at',
    'nodes_fts': 'node_type display_name description documentation operations',
    'templates_fts': 'name description',
}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Argument parsing must not echo arbitrary command-line input.
        self.exit(2, 'Invalid restore arguments\n')


def _read_regular(path: Path, *, product: str = 'generic') -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_size > 16 * 1024 * 1024):
            raise RestoreError()
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
            data = json.load(stream, object_pairs_hook=_unique_keys)
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        raise RestoreError()
    # Match ConfigStore's top-level and policy validation, without modifying the
    # original snapshot (ConfigStore's read tightens the source file's mode).
    for key, default in _DEFAULT_CONFIG.items():
        if key in data and not isinstance(data[key], type(default)):
            raise RestoreError()
    # Missing sections could be a truncated backup; applying runtime defaults
    # would silently remove restrictions. Require both explicitly.
    tools = data.get('tools')
    if not isinstance(tools, dict) or not {'disabled', 'disabled_operations'} <= tools.keys():
        raise RestoreError()
    read_policy(tools)
    if any(not isinstance(item, dict) or any(not isinstance(v, str) for v in item.values())
           for item in data.get('token_history', [])):
        raise RestoreError()
    for key in ('admin_password', 'admin_session_generation', 'mcp_auth_generation'):
        if key in data and not isinstance(data[key], str):
            raise RestoreError()
    if not data.get('admin_password'):
        raise RestoreError()
    server = data.get('mcp_server')
    if not isinstance(server, dict):
        raise RestoreError()
    port = server.get('port')
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise RestoreError()
    if ('env' in server and not isinstance(server['env'], dict)
            or 'args' in server and (not isinstance(server['args'], list)
                                     or any(not isinstance(arg, str) for arg in server['args']))
            or 'command' in server and not isinstance(server['command'], str)):
        raise RestoreError()
    if MARKER not in tools and product == 'generic':
        # Legacy inputs have no reliable schema discriminator. Only accept the
        # reviewed Odoo shape with all security switches explicitly present;
        # otherwise an app-specific startup default could open unknown rights.
        switches = ('ODOO_MCP_ENABLE_WRITES', 'ODOO_MCP_ALLOW_DIRECT_WRITES',
                    'ODOO_MCP_ALLOW_UNKNOWN_METHODS', 'MCP_CHATTER_DIRECT')
        env = server.get('env', {})
        if any(not isinstance(env.get(key), str) or env[key].strip() not in ('0', '1') for key in switches):
            raise RestoreError()
    proxy_bearer = data.get('proxy', {}).get('bearer_token')
    child_bearer = server.get('env', {}).get('AUTH_TOKEN')
    if product == 'n8n':
        # Both ends are required and must describe the same existing child.
        if (not isinstance(proxy_bearer, str) or not isinstance(child_bearer, str)
                or not proxy_bearer or len(proxy_bearer) > 512
                or any(not '!' <= c <= '~' for c in proxy_bearer)
                or not secrets.compare_digest(proxy_bearer.encode(), child_bearer.encode())):
            raise RestoreError()
    elif proxy_bearer or child_bearer:
        raise RestoreError()
    return data


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError()
        result[key] = value
    return result


def _directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise RestoreError()


def _read_only_mount(path: Path) -> bool:
    """A read-only mount point, e.g. a restored Kubernetes volume mounted readOnly."""
    try:
        return os.path.ismount(path) and bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _volume_root(path: Path) -> None:
    # A Kubernetes volume root is created by mkfs (root-owned) and only chgrp'd
    # by fsGroup. Accept it only as a read-only mount; contents are still checked.
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o002):
        raise RestoreError()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_config(path: Path, original: dict) -> None:
    config = copy.deepcopy(original)
    if MARKER not in config['tools']:
        config['tools'][RESTORE_POLICY_HOLD] = True
    config.update(admin_password=secrets.token_urlsafe(48),
                  mcp_auth_token='', mcp_auth_revoked=True,
                  mcp_auth_generation=secrets.token_urlsafe(32),
                  admin_session_generation=secrets.token_urlsafe(32), token_history=[])
    raw = json.dumps(config, ensure_ascii=False, indent=2).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _check_auxiliary_file(path: Path, max_bytes: int) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_size > max_bytes):
            raise RestoreError()
    finally:
        os.close(fd)


def _snapshot_dir(directory: Path, *, root: bool, product: str,
                  volume_root: bool = False) -> tuple[Path | None, Path | None, list[Path]]:
    allowed = {'config.json', '.config.json.operations.json', 'restore-operation-journals'}
    if product == 'n8n':
        allowed.add('n8n-mcp')
    if root:
        allowed.add('profiles')
    names = {path.name for path in directory.iterdir()}
    if root and volume_root and 'lost+found' in names:
        # ext4's own directory at a volume root is never read or copied.
        if not stat.S_ISDIR((directory / 'lost+found').lstat().st_mode):
            raise RestoreError()
        names.discard('lost+found')
    if root:
        for name in [n for n in names if _RESET_BACKUP.fullmatch(n) or n in _ADMIN_SESSIONS]:
            # lstat: a symlink, directory, FIFO etc. with this name is not the operator's or the service's own file
            # and stays unexpected. Skipped files are never opened, copied, published or counted.
            if stat.S_ISREG((directory / name).lstat().st_mode):
                names.discard(name)
    required = {'config.json', 'n8n-mcp'} if product == 'n8n' else {'config.json'}
    if not names <= allowed or not required <= names:
        raise RestoreError()
    journal = directory / '.config.json.operations.json' if '.config.json.operations.json' in names else None
    if journal:
        _check_auxiliary_file(journal, 262144)
    archives = []
    if 'restore-operation-journals' in names:
        archive_dir = directory / 'restore-operation-journals'
        _directory(archive_dir)
        archives = sorted(archive_dir.iterdir())
        if len(archives) > 128:
            raise RestoreError()
        for archive in archives:
            if not re.fullmatch(r'[0-9a-f]{32}\.(?:json|zip)', archive.name):
                raise RestoreError()
            _check_auxiliary_file(archive, 34 * 1024 * 1024 if archive.suffix == '.zip' else 262144)
    if product != 'n8n':
        return None, journal, archives
    database_dir = directory / 'n8n-mcp'
    _directory(database_dir)
    files = {path.name for path in database_dir.iterdir()}
    # A WAL/SHM or pending temporary file is not a complete offline snapshot.
    if not files <= {'nodes.db', '.seed.lock'} or 'nodes.db' not in files:
        raise RestoreError()
    if '.seed.lock' in files:
        _check_auxiliary_file(database_dir / '.seed.lock', 0)
    db = database_dir / 'nodes.db'
    _check_auxiliary_file(db, 2 * 1024 * 1024 * 1024)
    return db, journal, archives


def _copy_private_file(source: Path, destination: Path, max_bytes: int) -> None:
    src_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(src_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_size > max_bytes):
            raise RestoreError()
        dst_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(dst_fd, 'wb') as dest, os.fdopen(src_fd, 'rb', closefd=False) as src:
            os.fchmod(dest.fileno(), 0o600)
            shutil.copyfileobj(src, dest, length=1024 * 1024)
            dest.flush()
            os.fsync(dest.fileno())
        after = os.fstat(src_fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RestoreError()
    finally:
        os.close(src_fd)


def _copy_n8n_database(source: Path, destination: Path) -> None:
    _copy_private_file(source, destination, 2 * 1024 * 1024 * 1024)
    with closing(sqlite3.connect(destination.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if (db.execute('PRAGMA quick_check').fetchone() != ('ok',)
                or db.execute('PRAGMA user_version').fetchone() != (0,)
                or db.execute('PRAGMA application_id').fetchone() != (0,)):
            raise RestoreError()
        for table, required in _N8N_TABLE_COLUMNS.items():
            obj = db.execute('SELECT type FROM sqlite_master WHERE name = ?', (table,)).fetchone()
            columns = {row[1] for row in db.execute('SELECT * FROM pragma_table_info(?)', (table,))}
            if obj != ('table',) or not set(required.split()) <= columns:
                raise RestoreError()
        if not db.execute("SELECT 1 FROM nodes WHERE node_type IS NOT NULL AND node_type != '' LIMIT 1").fetchone():
            raise RestoreError()
        # Exercise the same FTS join used by catalog searches, not just schema
        # metadata; a missing or unusable index must prevent publication.
        if not db.execute("SELECT n.node_type FROM nodes_fts JOIN nodes n ON n.rowid=nodes_fts.rowid "
                          "WHERE nodes_fts MATCH 'n8n' LIMIT 1").fetchone():
            raise RestoreError()
        db.execute('SELECT id, instance_id, workflow_id, workflow_snapshot FROM workflow_versions LIMIT 1').fetchone()


def _check_n8n_runtime() -> None:
    # Use the same resolver and override as n8n-mcp-http. This validates the
    # installed package contract, not an arbitrary version claimed in backup.
    try:
        from n8n_mcp_admin import upstream

        package = Path(upstream.package_dir())
        with (package / 'package.json').open(encoding='utf-8') as stream:
            metadata = json.load(stream, object_pairs_hook=_unique_keys)
        if (not isinstance(metadata, dict) or metadata.get('name') != 'n8n-mcp' or metadata.get('version') != '2.96.2'
                or not (package / 'dist/mcp/index.js').is_file()):
            raise RestoreError()
    except (OSError, ValueError, TypeError, ImportError, subprocess.SubprocessError) as error:
        raise RestoreError() from error


def _archive_journal(source: Path, destination: Path) -> int:
    _copy_private_file(source, destination, 34 * 1024 * 1024 if source.suffix == '.zip' else 262144)
    if destination.suffix == '.zip':
        floor = 0
        with zipfile.ZipFile(destination) as bundle:
            entries = bundle.infolist()
            if not entries or len(entries) > 128 or len({entry.filename for entry in entries}) != len(entries):
                raise RestoreError()
            for entry in entries:
                if (not re.fullmatch(r'[0-9a-f]{32}\.json', entry.filename)
                        or entry.is_dir() or entry.file_size > 262144
                        or entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits != 0):
                    raise RestoreError()
                raw = bundle.read(entry)
                with tempfile.NamedTemporaryFile(dir=destination.parent) as temporary:
                    temporary.write(raw)
                    temporary.flush()
                    floor = max(floor, _read_journal_floor(Path(temporary.name)))
        return floor
    return _read_journal_floor(destination)


def _read_journal_floor(destination: Path) -> int:
    # Runtime's parser accepts repeated keys. Restore must reject them before
    # interpreting any cutoff or nested receipt, including archived ZIP members.
    with destination.open('r', encoding='utf-8') as stream:
        json.load(stream, object_pairs_hook=_unique_keys)
    # Reuse the runtime's complete journal schema validation on the private
    # staged copy; _read may chmod the copy, never the source snapshot.
    reader = OperationJournal(destination.parent / 'config.json')
    reader.path = destination
    value = reader._read()
    return max([value['expired_before_ms'], *(int(key.split('.', 1)[0]) for key in value['receipts'])])


def _compact_archives(directory: Path) -> None:
    entries = list(directory.iterdir())
    if len(entries) < 128:
        return
    # A legacy input can contain 128 archived JSON files plus an active journal.
    # Match the reader's 128-member bound and retain any remainder separately.
    journals = sorted(entry for entry in entries if entry.suffix == '.json')[:128]
    if len(journals) < 2:
        # The operator must export and retain an authenticated archive before
        # continuing. Never discard or overwrite an existing journal.
        raise RestoreError()
    bundle = directory / (secrets.token_hex(16) + '.zip')
    fd = os.open(bundle, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as output:
            for journal in journals:
                output.writestr(journal.name, journal.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    for journal in journals:
        journal.unlink()
    if len(list(directory.iterdir())) >= 128:
        raise RestoreError()


def _write_journal(path: Path, cutoff_ms: int) -> None:
    # A prior receipt could be replayed into restored state. Expire all IDs
    # issued before this restore instead of copying an old operation journal.
    value = {'version': 1, 'key': secrets.token_hex(32), 'expired_before_ms': cutoff_ms, 'receipts': {}}
    raw = json.dumps(value, separators=(',', ':')).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(source: Path, destination: Path) -> None:
    # Linux renameat2 prevents accidentally replacing even an empty existing dir.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise RestoreError() from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise OSError(ctypes.get_errno(), 'atomic publish failed')


def prepare(source: Path, destination: Path, *, product: str = 'generic',
            allow_volume_root: bool = False) -> None:
    """Validate *source* and atomically publish a fully revoked copy at *destination*.

    ``allow_volume_root`` (used by :mod:`restore_inplace`): when *source* is a
    read-only mount point, its root may be owned by root (Kubernetes fsGroup
    only changes the group) and may contain ext4's ``lost+found`` directory,
    which is ignored. Every other check is unchanged.
    """
    if product not in ('generic', 'n8n'):
        raise RestoreError()
    if product == 'n8n':
        _check_n8n_runtime()
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    volume_root = allow_volume_root and _read_only_mount(source)
    if volume_root:
        _volume_root(source)
    else:
        _directory(source)
    _directory(destination.parent)
    parent_info = destination.parent.stat()
    if parent_info.st_mode & 0o022 or destination == source or source in destination.parents:
        raise RestoreError()
    if destination.exists() or destination.is_symlink():
        raise RestoreError()
    root_db = _snapshot_dir(source, root=True, product=product, volume_root=volume_root)
    root = _read_regular(source / 'config.json', product=product)
    index = _validated_index(root)
    profiles = source / 'profiles'
    if index:
        _directory(profiles)
        if {p.name for p in profiles.iterdir()} != set(index):
            raise RestoreError()
    elif profiles.exists() or profiles.is_symlink():
        _directory(profiles)
        if any(profiles.iterdir()):
            raise RestoreError()
    configs = [('config.json', root, root_db)]
    for profile_id, item in index.items():
        directory = profiles / profile_id
        _directory(directory)
        database = _snapshot_dir(directory, root=False, product=product)
        config = _read_regular(directory / 'config.json', product=product)
        if config['mcp_server']['port'] != item['port'] or config.get('profile_index'):
            raise RestoreError()
        configs.append((f'profiles/{profile_id}/config.json', config, database))

    if product == 'n8n':
        used = {config['mcp_server']['env']['AUTH_TOKEN'] for _, config, _ in configs}
        used.update(config.get('mcp_auth_token', '') for _, config, _ in configs)
        rotated = []
        for relative, config, database in configs:
            for _ in range(16):
                bearer = secrets.token_urlsafe(32)
                if bearer not in used:
                    break
            else:
                raise RestoreError()
            used.add(bearer)
            fresh = copy.deepcopy(config)
            fresh['mcp_server']['env']['AUTH_TOKEN'] = bearer
            fresh['proxy']['bearer_token'] = bearer
            rotated.append((relative, fresh, database))
        configs = rotated

    stage = Path(tempfile.mkdtemp(prefix='.mcp-restore-', dir=destination.parent))
    try:
        os.chmod(stage, 0o700)
        journal_floors = {}
        for relative, config, snapshot in configs:
            journal_floors[relative] = 0
            target = stage / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_config(target, config)
            if snapshot is not None:
                database, journal, archives = snapshot
                if database is not None:
                    db_dir = target.parent / 'n8n-mcp'
                    db_dir.mkdir(mode=0o700)
                    _copy_n8n_database(database, db_dir / 'nodes.db')
                    _fsync_dir(db_dir)
                if journal or archives:
                    archive_dir = target.parent / 'restore-operation-journals'
                    archive_dir.mkdir(mode=0o700)
                    for archive in archives:
                        journal_floors[relative] = max(
                            journal_floors[relative], _archive_journal(archive, archive_dir / archive.name))
                    if journal:
                        for _ in range(16):
                            archived = archive_dir / (secrets.token_hex(16) + '.json')
                            if not archived.exists():
                                break
                        else:
                            raise RestoreError()
                        journal_floors[relative] = max(journal_floors[relative], _archive_journal(journal, archived))
                    _compact_archives(archive_dir)
                    _fsync_dir(archive_dir)
        # Expire stale IDs only after all validated data is staged; retaining
        # the original journal archive makes this rollover auditable.
        cutoff_ms = int(time.time() * 1000)
        for relative, _, _ in configs:
            _write_journal((stage / relative).parent / '.config.json.operations.json',
                           max(cutoff_ms, journal_floors[relative]))
        if index:
            for profile_id in index:
                _fsync_dir(stage / 'profiles' / profile_id)
            _fsync_dir(stage / 'profiles')
        _fsync_dir(stage)
        _publish(stage, destination)
        _fsync_dir(destination.parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv=None) -> int:
    parser = _Parser(description='Prepare a supported MCP restore offline')
    parser.add_argument('--product', choices=('generic', 'n8n'), default='generic')
    parser.add_argument('mode', choices=('prepare', 'serve'))
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if (args.mode == 'serve') != bool(command):
        parser.error('serve requires a command; prepare accepts none')
    try:
        prepare(args.source, args.destination, product=args.product)
        if args.mode == 'serve':
            os.environ['MCP_ADMIN_CONFIG'] = str(args.destination.absolute() / 'config.json')
            os.execvp(command[0], command)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error, zipfile.BadZipFile,
            ConfigUnavailable, RestoreError):
        # No paths, parsed JSON, subprocess output or exception text: they may
        # contain credentials. The orchestrator must keep the public gate closed.
        print('MCP restore preparation failed; keep service isolated', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
