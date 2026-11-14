"""Credential masking before handlers, preserving Uvicorn access-log arguments."""
import base64
import json
import logging
import threading
from urllib.parse import quote
from mcp_admin_core import log_redaction as core

_values = ()
_lock = threading.Lock()
_installed = False


def set_secrets(key, secret):
    global _values
    values = {v for v in (key, secret) if v}
    if key and secret:
        values.add(base64.b64encode(f'{key}:{secret}'.encode()).decode())
    values |= {quote(v, safe='') for v in values} | {json.dumps(v)[1:-1] for v in values}
    with _lock:
        # Retain recently rotated values so buffered child output stays masked.
        _values = tuple(sorted(dict.fromkeys([*values, *_values[:96]]), key=len, reverse=True))
    install()


def scrub(value):
    if isinstance(value, str):
        for secret in _values:
            value = value.replace(secret, '***')
    elif isinstance(value, tuple):
        value = tuple(scrub(v) for v in value)
    elif isinstance(value, dict):
        value = {k: scrub(v) for k, v in value.items()}
    elif isinstance(value, list):
        value = [scrub(v) for v in value]
    return value


def install():
    global _installed
    core.install()
    with _lock:
        if _installed:
            return
        previous = logging.getLogRecordFactory()
        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            record.msg, record.args = scrub(record.msg), scrub(record.args)
            if record.exc_info:
                record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            if record.exc_text:
                record.exc_text = scrub(record.exc_text)
            if record.stack_info:
                record.stack_info = scrub(record.stack_info)
            # The exception was rendered again from raw exc_info above. Apply
            # the shared URL policy after the EMQX-specific substitutions.
            core.scrub_record(record)
            return record
        logging.setLogRecordFactory(factory)
        _installed = True
