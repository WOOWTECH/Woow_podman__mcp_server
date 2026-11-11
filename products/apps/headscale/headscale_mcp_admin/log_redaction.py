"""Redact configured upstream credentials from server and dependency logs."""
import logging
import threading

_secrets: set[str] = set()
_lock = threading.Lock()
_installed = False


def set_secrets(*values):
    # Retain old credentials for late log messages from in-flight requests.
    with _lock:
        _secrets.update(str(v) for v in values if v)


def install():
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        with _lock:
            secrets = sorted(_secrets, key=len, reverse=True)

        def scrub(value):
            if isinstance(value, tuple):
                return tuple(scrub(item) for item in value)
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if value is None or isinstance(value, (bool, int, float)):
                return value
            text = str(value)
            cleaned = text
            for secret in secrets:
                cleaned = cleaned.replace(secret, "[credential]")
            return value if cleaned == text else cleaned

        # Uvicorn's access formatter unpacks five positional arguments. Preserve
        # their types and structure instead of flattening the formatted message.
        record.msg, record.args = scrub(record.msg), scrub(record.args)
        if record.exc_info and not record.exc_text:
            exception = logging.Formatter().formatException(record.exc_info)
            cleaned = scrub(exception)
            if cleaned != exception:
                record.exc_text = cleaned
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
