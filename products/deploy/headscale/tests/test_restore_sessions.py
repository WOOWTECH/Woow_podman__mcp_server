"""An older backup must never reactivate a logged-out GUI session."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/rotate-restored-sessions.py"
spec = importlib.util.spec_from_file_location("rotate_restored_sessions", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_restored_epoch_changes_and_old_revocations_are_removed(tmp_path):
    path = tmp_path / "admin-sessions.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE session_state (id INTEGER PRIMARY KEY, epoch TEXT NOT NULL)")
        db.execute("INSERT INTO session_state VALUES (1, ?)", ("a" * 32,))
        db.execute("CREATE TABLE revoked_sessions (jti_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
        db.execute("INSERT INTO revoked_sessions VALUES ('old', 9999999999)")
    assert module.rotate(path) is True
    with sqlite3.connect(path) as db:
        epoch = db.execute("SELECT epoch FROM session_state WHERE id=1").fetchone()[0]
        assert epoch != "a" * 32
        assert len(epoch) == 32
        assert db.execute("SELECT COUNT(*) FROM revoked_sessions").fetchone()[0] == 0


def test_missing_database_is_unchanged(tmp_path):
    path = tmp_path / "missing.sqlite3"
    assert module.rotate(path) is False
    assert not path.exists()


def test_malformed_database_is_rejected(tmp_path):
    path = tmp_path / "malformed.sqlite3"
    path.write_text("invalid")
    with pytest.raises(sqlite3.DatabaseError):
        module.rotate(path)
