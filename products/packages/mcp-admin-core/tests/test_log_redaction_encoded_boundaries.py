"""Regression at the rendered logging boundary; all credentials are synthetic."""
import logging

import pytest

from mcp_admin_core import log_redaction


@pytest.mark.parametrize('target', [
    '/api/profiles?%2574oken={secret}&token={secret}',
    '/api/profiles?%2574oken={secret}',
    '/api/profiles?token%3D{secret}',
    '/api/profiles?x=1%26token={secret}',
    '/api/profiles?x=1;token={secret}',
    '/api/profiles?%6Goken={secret}',
    '/api/profiles?%2561ccess_token={secret}',
    '/api/profiles?token%0A={secret}',
    '/api/profiles?to%00ken={secret}',
    '/api/profiles?token%23={secret}',
    '/api/profiles?token%20={secret}',
    '/api/profiles?to%2Dken={secret}',
    '/api/profiles?token%3A={secret}',
    '/api/profiles?token%3B={secret}',
    '/api/profiles?token%26={secret}',
    '/api/profiles?token%20={secret}&token={secret}',
    '/%2570rivate_{secret}/mcp',
])
def test_rejected_and_accepted_raw_targets_do_not_expose_secret(target):
    secret = 'synthetic-canary-not-a-credential'
    assert (secret in log_redaction.redact(target.format(secret=secret))) is False


def test_deep_ambiguous_encoding_fails_closed():
    secret = 'synthetic-canary-not-a-credential'
    nested = '%' + '25' * 20 + '74oken=' + secret
    assert (secret in log_redaction.redact('/api/profiles?' + nested)) is False


@pytest.mark.parametrize(('template', 'arguments'), [
    ('GET %s%s', ('/api/logs?token=', 'synthetic-canary-not-a-credential')),
    ('GET /api/logs?token=%s', ('synthetic-canary-not-a-credential',)),
])
def test_rendered_split_arguments_do_not_leak_or_break_formatting(template, arguments):
    record = logging.LogRecord('test.synthetic', logging.INFO, __file__, 1, template, arguments, None)
    log_redaction.scrub_record(record)
    assert ('synthetic-canary-not-a-credential' in record.getMessage()) is False
