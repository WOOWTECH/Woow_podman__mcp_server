"""以 launcher 呼叫點與真 SQLite 驗證 catalog/工作流歷史持久性。"""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from n8n_mcp_admin import launcher, upstream


@pytest.fixture(autouse=True)
def forbid_unobserved_exec(monkeypatch):
    def unexpected_exec(*args):
        raise AssertionError("launcher attempted Node exec before database safety checks")
    monkeypatch.setattr(launcher.os, "execvpe", unexpected_exec)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    package = tmp_path / "package"
    (package / "dist/mcp").mkdir(parents=True)
    (package / "dist/mcp/index.js").write_text("// fixture")
    (package / "data").mkdir()
    source = package / "data/nodes.db"
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE nodes(node_type TEXT PRIMARY KEY, documentation TEXT);"
                         "INSERT INTO nodes VALUES('nodes-base.manualTrigger', '完整節點資料');"
                         "CREATE TABLE workflow_versions(id INTEGER PRIMARY KEY, workflow_snapshot TEXT);")
    source.chmod(0o444)
    config = tmp_path / "persistent/config.json"
    config.parent.mkdir()
    config.write_text("{}")
    monkeypatch.setenv("MCP_ADMIN_CONFIG", str(config))
    monkeypatch.setattr(upstream, "package_dir", lambda: str(package))
    return source, config


def launch(monkeypatch):
    captured = {}
    def exec_node(command, argv, env):
        captured.update(command=command, argv=argv, env=env)
        raise SystemExit(0)
    monkeypatch.setattr(launcher.os, "execvpe", exec_node)
    with pytest.raises(SystemExit) as ex:
        launcher.main([])
    assert ex.value.code == 0
    return captured


def test_launcher_real_sqlite_preupdate_snapshots_survive_relaunch(catalog, monkeypatch):
    source, config = catalog
    original = source.read_bytes()
    result = launch(monkeypatch)
    path = Path(result["env"]["NODE_DB_PATH"])
    assert path == config.parent / "n8n-mcp/nodes.db"
    assert path.is_file() and path != source
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT documentation FROM nodes").fetchone()[0] == '完整節點資料'
        for value in [1, 2]:
            db.execute("INSERT INTO workflow_versions(workflow_snapshot) VALUES(?)", (json.dumps({"value": value}),))
    again = launch(monkeypatch)
    assert again["env"]["NODE_DB_PATH"] == str(path)
    with sqlite3.connect(path) as db:
        assert [json.loads(row[0])["value"] for row in db.execute("SELECT workflow_snapshot FROM workflow_versions ORDER BY id")] == [1, 2]
    assert source.read_bytes() == original
    assert result["env"]["N8N_MCP_TELEMETRY_DISABLED"] == 'true'


def test_fresh_process_keeps_history(catalog, monkeypatch):
    source, config = catalog
    result = launch(monkeypatch)
    path = Path(result["env"]["NODE_DB_PATH"])
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO workflow_versions(workflow_snapshot) VALUES('{\"value\":2}')")
    script = "from n8n_mcp_admin.database import prepare_database; import sys; print(prepare_database(sys.argv[1], sys.argv[2]))"
    child = subprocess.run([sys.executable, '-c', script, str(source.parent.parent), str(config)], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == str(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_versions").fetchone()[0] == 1


def test_parallel_first_seed_does_not_clobber(catalog):
    from n8n_mcp_admin.database import prepare_database
    source, config = catalog
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: prepare_database(str(source.parent.parent), str(config)), range(8)))
    assert len(set(paths)) == 1
    with sqlite3.connect(paths[0]) as db:
        assert db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0] == 1
    assert not list(Path(paths[0]).parent.glob('*.tmp'))


def test_existing_corrupt_database_is_preserved_and_refused(catalog, monkeypatch):
    source, config = catalog
    target = config.parent / 'n8n-mcp/nodes.db'
    target.parent.mkdir()
    target.write_bytes(b'corrupt-keep-evidence')
    with pytest.raises(SystemExit):
        launcher.main([])
    assert target.read_bytes() == b'corrupt-keep-evidence'


def test_existing_readonly_database_is_refused(catalog, monkeypatch):
    source, config = catalog
    path = Path(launch(monkeypatch)['env']['NODE_DB_PATH'])
    path.chmod(0o444)
    original = path.read_bytes()
    try:
        with pytest.raises(SystemExit) as ex:
            launcher.main([])
        assert '資料庫' in str(ex.value)
        assert path.read_bytes() == original
    finally:
        path.chmod(0o600)


def test_symlink_target_is_refused(catalog, monkeypatch):
    source, config = catalog
    target = config.parent / 'n8n-mcp/nodes.db'
    target.parent.mkdir()
    target.symlink_to(source)
    with pytest.raises(SystemExit):
        launcher.main([])
    assert target.is_symlink() and source.exists()


def test_missing_seed_does_not_publish_empty_database(catalog, monkeypatch):
    source, config = catalog
    source.unlink()
    with pytest.raises(SystemExit):
        launcher.main([])
    assert not (config.parent / 'n8n-mcp/nodes.db').exists()
