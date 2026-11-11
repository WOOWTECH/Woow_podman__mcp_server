"""Actual pinned handler and official dispatcher; synthetic transport, no backend."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from n8n_mcp_admin import upstream_direct_patch as direct
from n8n_mcp_admin import upstream_message_patch as message

APP = Path(__file__).resolve().parents[1]
FIXTURES = APP / 'tests/fixtures'
NATIVE = APP / 'upstream-dependencies/node_modules/n8n-mcp'
SCENARIOS = [
    'formats', 'formats-zod3', 'client-isolation', 'no-inputs', 'failed-retry', 'retry-timeout',
    'failed-retry-not-cached', 'changed-client-in-flight', 'cached-error-no-retry',
    'exposure-denied', 'other-methods', 'wrong-path', 'wrong-code', 'wrong-options',
    'wrong-discriminator', 'wrong-note', 'nested-union-errors',
    'multiple-issues', 'business-error', 'plain-phrase', 'wrong-tool', 'malformed-json',
    'truncated-json', 'authorization', 'execution-error', 'timeout',
]


def test_native_dispatcher_is_the_reviewed_pinned_dependency():
    assert (NATIVE / 'package.json').is_file(), (
        'Install the locked native fixture: npm ci --prefix apps/n8n/upstream-dependencies '
        '--omit=dev --ignore-scripts --no-audit --no-fund'
    )
    assert json.loads((NATIVE / 'package.json').read_text())['version'] == '2.96.2'
    assert hashlib.sha256((NATIVE / 'dist/mcp/handlers-official-tools.js').read_bytes()).hexdigest() == \
        'e85937c58d5a8426d54b1b69598adc83a14648402fc58a8714ef5db8132977cd'


@pytest.fixture
def compiled(tmp_path):
    (tmp_path / 'package.json').write_text(json.dumps({'version': direct.VERSION}))
    handler = tmp_path / direct.HANDLER
    handler.parent.mkdir(parents=True)
    handler.write_bytes(gzip.decompress((FIXTURES / 'n8n-mcp-2.96.2-handler.js.gz').read_bytes()))
    direct.patch_package(tmp_path)
    message.patch_package(tmp_path)
    return handler


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_actual_native_handler_and_dispatcher(compiled, scenario):
    run = subprocess.run(['node', str(FIXTURES / 'native-direct-probe.cjs'), str(compiled),
                          scenario, str(NATIVE)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout.splitlines()[-1])['passed'] is True
