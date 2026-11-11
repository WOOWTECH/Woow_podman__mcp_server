"""n8n child 必須用原生 better-sqlite3，不能靜默退回 sql.js（Node 24／26 沒有 n8n-mcp 宣告的 better-sqlite3 11.x 預建檔）。

- ``sqlite_check.main`` 只有在上游選到 ``BetterSQLiteAdapter``、原生模組真的載入行程、FTS5 查得到種子 catalog、
  版本等於釘版時才回 0；
- 有上游套件時（映像內，或本機以 ``N8N_MCP_PACKAGE_DIR`` 指到 ``npm ci`` 後的 n8n-mcp）實際跑一次探針
  （映像建置時 Dockerfile 也會跑）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from n8n_mcp_admin import sqlite_check

APP = Path(__file__).resolve().parents[1]
PINNED = json.loads((APP / "upstream-dependencies" / "package.json").read_text())["overrides"]["better-sqlite3"]

NATIVE = {"node": "v26.12.1", "abi": "147", "betterSqlite3": PINNED, "adapter": "BetterSQLiteAdapter",
          "nativeMapped": True, "fts5": True, "ftsMatch": True, "sqlite": "3.53.2"}
# Node 26.12.1 + better-sqlite3 11.10.0（沒有 ABI 147 預建檔，npm ci 靜默略過）時探針的實際輸出。
SQLJS_FALLBACK = {"node": "v26.12.1", "abi": "147", "betterSqlite3": None, "adapter": "SQLJSAdapter",
                  "nativeMapped": False, "fts5": False, "ftsMatch": False, "sqlite": "3.54.0"}


@pytest.mark.parametrize(
    "data, rc",
    [
        (NATIVE, 0),
        (SQLJS_FALLBACK, 1),
        ({**NATIVE, "betterSqlite3": "11.10.0"}, 1),  # 裝到的不是釘版
        ({**NATIVE, "nativeMapped": False}, 1),  # adapter 名稱對，但原生模組沒有進行程
        ({**NATIVE, "ftsMatch": False}, 1),  # FTS5 查不到種子 catalog
    ],
)
def test_main_requires_native_better_sqlite3(monkeypatch, capsys, data, rc) -> None:
    monkeypatch.setattr(sqlite_check, "probe", lambda package_dir=None: dict(data))
    assert sqlite_check.main(["--expect-version", PINNED, "/x/n8n-mcp"]) == rc
    assert ("NOT native" in capsys.readouterr().out) == bool(rc)


def _package_dir() -> str | None:
    override = os.environ.get("N8N_MCP_PACKAGE_DIR")
    if override:
        return override if os.path.isdir(override) else None
    if not shutil.which("npm"):
        return None
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    path = os.path.join(root.stdout.strip(), "n8n-mcp")
    return path if os.path.isdir(path) else None


def test_upstream_adapter_is_native_better_sqlite3() -> None:
    pkg = _package_dir()
    if not pkg or not shutil.which("node"):
        pytest.skip("n8n-mcp 套件不在本機（映像建置時 Dockerfile 會跑 sqlite_check）")
    data = sqlite_check.probe(pkg)
    want = sqlite_check.expected(PINNED)
    assert {key: data.get(key) for key in want} == want, data
    assert sqlite_check.main(["--expect-version", PINNED, pkg]) == 0


def test_image_build_and_ci_smoke_enforce_native_sqlite() -> None:
    """映像建置跑 sqlite_check、CI 的 child smoke 檢查真正的 child；拿掉任何一道都要在這裡失敗。"""
    dockerfile = (APP / "Dockerfile").read_text()
    assert 'python -m n8n_mcp_admin.sqlite_check --expect-version "${BETTER_SQLITE3_VERSION}"' in dockerfile
    assert "node_modules/better-sqlite3/package.json').version\")\" = \"${BETTER_SQLITE3_VERSION}\"" in dockerfile
    smoke = (APP / "scripts" / "ci_smoke_child.py").read_text()
    assert "assert native_sqlite_loaded(proc.pid)" in smoke
