"""Guard the real published npm artifact and reject unreviewed upstream changes."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import stat
import subprocess

import pytest

from n8n_mcp_admin import upstream_direct_patch as patch

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """Unpack the actual pinned compiled handler without executing npm scripts."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    data = gzip.decompress((FIXTURES / f"n8n-mcp-{manifest['version']}-handler.js.gz").read_bytes())
    assert hashlib.sha256(data).hexdigest() == manifest["handler_sha256"]
    (tmp_path / "package.json").write_text(json.dumps({"version": manifest["version"]}))
    handler = tmp_path / patch.HANDLER
    handler.parent.mkdir(parents=True)
    handler.write_bytes(data)
    handler.chmod(0o644)
    return tmp_path


def test_published_handler_only_changes_the_execute_workflow_call(package: Path) -> None:
    """All original bytes outside the one reviewed call site stay identical."""
    handler = package / patch.HANDLER
    original = handler.read_bytes()
    assert patch.patch_package(package) is True
    result = handler.read_bytes()
    assert hashlib.sha256(result).hexdigest() == patch.PATCHED_SHA256
    for before, after in patch.REPLACEMENTS:
        assert result.count(after) == 1
        result = result.replace(after, before, 1)
    assert result == original
    assert stat.S_IMODE(handler.stat().st_mode) == 0o644


def test_verified_patch_is_idempotent_and_does_not_replace_file(package: Path) -> None:
    """Repeat build checks accept the known result without rewriting it."""
    assert patch.patch_package(package) is True
    handler = package / patch.HANDLER
    before = handler.stat()
    assert patch.patch_package(package) is False
    after = handler.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_unknown_version_rejected_before_mutation(package: Path) -> None:
    """Changing the Docker build ARG requires reviewing this compatibility patch."""
    (package / "package.json").write_text('{"version":"2.96.3"}')
    handler = package / patch.HANDLER
    before = handler.read_bytes()
    with pytest.raises(ValueError, match="version"):
        patch.patch_package(package)
    assert handler.read_bytes() == before


def test_unknown_handler_bytes_rejected_before_mutation(package: Path) -> None:
    """Even unchanged version metadata must not mask an upstream content change."""
    handler = package / patch.HANDLER
    altered = handler.read_bytes() + b"\n// unreviewed change\n"
    handler.write_bytes(altered)
    with pytest.raises(ValueError, match="bytes"):
        patch.patch_package(package)
    assert handler.read_bytes() == altered


def test_missing_handler_fails_instead_of_silent_success(package: Path) -> None:
    """Package layout changes are a build failure."""
    handler = package / patch.HANDLER
    handler.unlink()
    with pytest.raises(FileNotFoundError):
        patch.patch_package(package)


def test_symlink_handler_is_rejected(package: Path) -> None:
    """Never follow a handler symlink to a different artifact."""
    handler = package / patch.HANDLER
    target = package / "other.js"
    target.write_bytes(handler.read_bytes())
    handler.unlink()
    handler.symlink_to(target)
    with pytest.raises(ValueError, match="regular"):
        patch.patch_package(package)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == patch.ORIGINAL_SHA256


def test_patched_call_tries_untagged_first_and_retries_tagged_only_on_discriminator_error(package: Path) -> None:
    """DATA-NMCP-001-1：n8n 2.43.x 不收 inputs.type、2.35.x 要求它。

    * 第一次用「未加標籤」（上游原樣＝2.43.x 的 schema）；
    * 只有精確 SDK inputs.type discriminator 錯誤才用 type 標籤重試一次；
    * 成功形式只記在同一個 official client，client 更換後重新協商。
    以前的版本永遠加標籤：在 2.43.x 回 "Unrecognized key(s) in object: 'type'"。
    """
    assert patch.patch_package(package) is True
    text = (package / patch.HANDLER).read_text()
    assert "woowDirectInputFormats = new WeakMap()" in text
    assert "execute(knownFormat === true)" in text
    assert "result.code !== 'INVALID_ARGS'" in text
    assert "missingInputTag(response)" in text
    assert "__woowDirectTagged" not in text
    assert "type: 'chatInput' in inputs ? 'chat' : 'formData' in inputs ? 'form' : 'webhook'" in text
    # 沒有無條件加標籤的舊 patch 殘留
    assert "inputs = { type: 'chat'" not in text and "type: 'webhook',\n" not in text


@pytest.mark.parametrize("scenario", [
    "retry-timeout", "retry-backend-error", "retry-throws", "retry-success", "unrelated-error",
])
def test_actual_handler_preserves_retry_outcome_without_duplicate_execution(package: Path, scenario: str) -> None:
    patch.patch_package(package)
    run = subprocess.run(
        ["node", str(FIXTURES / "direct-retry-probe.cjs"), str(package / patch.HANDLER), scenario],
        capture_output=True, text=True, timeout=20,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["passed"] is True
