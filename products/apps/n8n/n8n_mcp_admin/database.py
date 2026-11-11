"""在可寫持久目錄首次初始化 n8n-mcp catalog 與工作流歷史。"""
from __future__ import annotations

from contextlib import closing
import fcntl
import os
from pathlib import Path
import sqlite3
import tempfile


def _validate_database(path: Path) -> None:
    """讀取完整 catalog，並真的寫入/回滾以檢查 DB 及 journal 權限。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("資料庫必須是普通檔案")
    # 高權限容器能繞過 chmod 0444，仍須尊重明示的唯讀設定。
    if not path.stat().st_mode & 0o222:
        raise PermissionError("資料庫已標示為唯讀")
    with closing(sqlite3.connect(path, timeout=10)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("資料庫完整性檢查失敗")
        if not db.execute("SELECT 1 FROM nodes LIMIT 1").fetchone():
            raise ValueError("資料庫缺少節點 catalog")
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE __woow_write_probe(value INTEGER)")
            db.execute("INSERT INTO __woow_write_probe VALUES(1)")
        finally:
            db.rollback()


def prepare_database(package_dir: str, config_path: str) -> str:
    """只在不存在時複製完整種子；既有歷史永不覆蓋，故障則拒絕啟動。"""
    directory = Path(config_path).absolute().parent / "n8n-mcp"
    if directory.is_symlink():
        raise ValueError("資料庫目錄不能是符號連結")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / "nodes.db"
    lock_fd = os.open(directory / ".seed.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # lexists 讓 dangling symlink 也被拒絕，不能當作尚未初始化。
        if not os.path.lexists(target):
            source = Path(package_dir) / "data/nodes.db"
            if source.is_symlink() or not source.is_file():
                raise ValueError("上游節點資料庫不存在")
            descriptor, temporary = tempfile.mkstemp(prefix="nodes-", suffix=".tmp", dir=directory)
            os.close(descriptor)
            temporary_path = Path(temporary)
            try:
                # 使用 SQLite backup，保留全 catalog/schema，而非建立空 DB。
                with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as seed:
                    with closing(sqlite3.connect(temporary_path)) as copied:
                        seed.backup(copied)
                _validate_database(temporary_path)
                with temporary_path.open("rb") as copied:
                    os.fsync(copied.fileno())
                os.replace(temporary_path, target)
                dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                temporary_path.unlink(missing_ok=True)
        _validate_database(target)
    return str(target)
