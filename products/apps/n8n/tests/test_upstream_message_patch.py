"""Compose the message fix with the current direct patch, using real npm bytes."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from n8n_mcp_admin import upstream_direct_patch as direct
from n8n_mcp_admin import upstream_message_patch as patch

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture
def package(tmp_path):
    manifest = json.loads((FIXTURES / 'manifest.json').read_text())
    original = gzip.decompress((FIXTURES / 'n8n-mcp-2.96.2-handler.js.gz').read_bytes())
    assert hashlib.sha256(original).hexdigest() == manifest['handler_sha256']
    (tmp_path / 'package.json').write_text(json.dumps({'version': manifest['version']}))
    target = tmp_path / patch.HANDLER
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    target.chmod(0o644)
    direct.patch_package(tmp_path)
    return tmp_path


def test_compiled_handler_preserves_backend_messages(package):
    assert patch.patch_package(package) is True
    run = subprocess.run(['node', str(FIXTURES / 'message-patch-probe.cjs'), str(package / patch.HANDLER)],
                         capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)['cases'] == 26


def test_combined_patch_changes_only_reviewed_error_branch(package):
    target = package / patch.HANDLER
    before = target.read_bytes()
    assert hashlib.sha256(before).hexdigest() == direct.PATCHED_SHA256 == patch.ORIGINAL_SHA256
    patch.patch_package(package)
    after = target.read_bytes()
    assert hashlib.sha256(after).hexdigest() == patch.PATCHED_SHA256
    for old, new in patch.REPLACEMENTS:
        assert after.count(new) == 1
        after = after.replace(new, old, 1)
    assert after == before
    for _, new in direct.REPLACEMENTS:
        assert new in after  # original direct behavior remains byte-identical
    assert target.stat().st_mode & 0o777 == 0o644
    subprocess.run(['node', '--check', str(target)], check=True, capture_output=True, timeout=20)


def test_idempotent_without_rewrite(package):
    patch.patch_package(package)
    before = (package / patch.HANDLER).stat()
    assert patch.patch_package(package) is False
    after = (package / patch.HANDLER).stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


@pytest.mark.parametrize('kind', ['version', 'pristine', 'drift', 'symlink'])
def test_reject_unreviewed_inputs_before_mutation(package, kind):
    target = package / patch.HANDLER
    if kind == 'version':
        (package / 'package.json').write_text('{"version":"unreviewed"}')
    elif kind == 'pristine':
        target.write_bytes(gzip.decompress((FIXTURES / 'n8n-mcp-2.96.2-handler.js.gz').read_bytes()))
    elif kind == 'drift':
        target.write_bytes(target.read_bytes() + b'\n// drift')
    else:
        regular = target.with_suffix('.regular.js')
        target.rename(regular)
        target.symlink_to(regular)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        patch.patch_package(package)
    assert target.read_bytes() == before


def test_build_order_is_direct_then_message_then_result():
    dockerfile = (Path(__file__).parents[1] / 'Dockerfile').read_text()
    assert dockerfile.index('RUN python /tmp/upstream_direct_patch.py') < dockerfile.index('RUN python /tmp/upstream_message_patch.py') < dockerfile.index('RUN python /tmp/upstream_result_patch.py')
