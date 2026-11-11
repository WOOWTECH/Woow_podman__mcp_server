#!/usr/bin/env python3
"""Invalidate GUI cookies after restoring an older Headscale session database."""

from __future__ import annotations

import secrets
import sqlite3
import sys
from pathlib import Path


def rotate(path: Path) -> bool:
    if not path.exists():
        return False  # The app will create a fresh session database on startup.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True) as db:
        row = db.execute("SELECT epoch FROM session_state WHERE id=1").fetchone()
        if row is None or not isinstance(row[0], str) or len(row[0]) != 32:
            raise ValueError("invalid restored session database")
        db.execute("UPDATE session_state SET epoch=? WHERE id=1", (secrets.token_hex(16),))
        db.execute("DELETE FROM revoked_sessions")
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: rotate-restored-sessions.py /path/to/admin-sessions.sqlite3")
    rotate(Path(sys.argv[1]))
