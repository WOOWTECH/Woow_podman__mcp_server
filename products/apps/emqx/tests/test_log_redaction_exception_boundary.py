"""EMQX's wrapper must preserve the core URL scrubber for ordinary loggers."""
import subprocess
import sys


def test_exception_text_does_not_restore_encoded_url_secret():
    code = '''
import logging
from emqx_mcp_admin import log_redaction
log_redaction.install()
try:
    raise ValueError("/api/profiles?%2574oken=synthetic-emqx-canary")
except ValueError:
    record = logging.getLogger("ordinary.emqx").makeRecord(
        "ordinary.emqx", logging.ERROR, "synthetic", 1, "error", (), __import__("sys").exc_info())
print("synthetic-emqx-canary" not in logging.Formatter().format(record))
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'True'
