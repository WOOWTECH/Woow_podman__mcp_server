import pytest

@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch):
    monkeypatch.setenv('EMQX_MCP_BASE_URL', 'http://127.0.0.1:9')
    monkeypatch.setenv('EMQX_MCP_API_KEY', 'synthetic-api-key')
    monkeypatch.setenv('EMQX_MCP_API_SECRET', 'synthetic-api-secret')
    monkeypatch.setenv('WEBHOOK_SECURITY_MODE', 'moderate')
    monkeypatch.setenv('FASTMCP_SHOW_SERVER_BANNER', 'false')
    monkeypatch.setenv('FASTMCP_CHECK_FOR_UPDATES', 'off')

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

@pytest.fixture
def broker(monkeypatch):
    state = SimpleNamespace(calls=[], status=200, response=None, headers={}, body=None, delay=0, routes={})
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.respond()
        def do_POST(self):
            self.respond()
        def do_PUT(self):
            self.respond()
        def do_DELETE(self):
            self.respond()
        def respond(self):
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else b''
            state.calls.append((self.command, self.path, body, self.headers.get('Authorization')))
            if state.delay:
                import time
                time.sleep(state.delay)
            if self.headers.get('Authorization') != 'Basic ' + base64.b64encode(b'synthetic-api-key:synthetic-api-secret').decode():
                status, value = 401, {'error': 'bad credentials'}
            else:
                status = state.status
                from urllib.parse import urlsplit
                value = state.routes.get(urlsplit(self.path).path, state.response)
                if value is None:
                    value = ([{'node': 'emqx@owned', 'version': '5.8.9'}] if self.path.endswith('/nodes') else
                             [{'connections.count': 7}] if self.path.endswith('/stats') else
                             {'data': [], 'meta': {'count': 0}})
            data = state.body if state.body is not None else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for k, v in state.headers.items():self.send_header(k, v)
            self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    state.url = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('EMQX_MCP_BASE_URL', state.url)
    yield state
    server.shutdown();server.server_close();thread.join(timeout=2)
