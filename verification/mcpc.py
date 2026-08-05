"""Tiny in-pod MCP + admin-API client for the verification harness."""
import json, os, urllib.request, urllib.error

CFG = json.load(open(os.environ.get('MCP_ADMIN_CONFIG', '/data/config.json')))
TOKEN = CFG['mcp_auth_token']
ADMIN = 'http://127.0.0.1:8080'
MCP_URL = ADMIN + '/private_' + TOKEN + '/mcp/'
UA = 'Mozilla/5.0 (X11; Linux x86_64) verify-harness/1.0'


def _req(url, data=None, headers=None, method=None):
    h = {'User-Agent': UA}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h.setdefault('Content-Type', 'application/json')
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8', 'replace')


_JWT = None


def api_token():
    global _JWT
    if _JWT is None:
        s, _, b = _req(ADMIN + '/api/auth/login', {'password': CFG['admin_password']})
        _JWT = json.loads(b)['token']
    return _JWT


def api(path, data=None, method=None):
    s, _, b = _req(ADMIN + path, data,
                   {'Authorization': 'Bearer ' + api_token()}, method)
    try:
        return s, json.loads(b)
    except Exception:
        return s, b


def _sse(text):
    """Pull the first JSON payload out of an SSE (or plain JSON) response."""
    for line in text.splitlines():
        if line.startswith('data:'):
            return json.loads(line[5:].strip())
    return json.loads(text) if text.strip() else None


class Session:
    """Stateful streamable-HTTP MCP session: initialize -> initialized -> calls."""

    def __init__(self, url=MCP_URL):
        self.url = url
        self.sid = None
        self.n = 0

    def _post(self, payload):
        h = {'Accept': 'application/json, text/event-stream'}
        if self.sid:
            h['Mcp-Session-Id'] = self.sid
        st, hd, body = _req(self.url, payload, h)
        sid = hd.get('Mcp-Session-Id') or hd.get('mcp-session-id')
        if sid:
            self.sid = sid
        return st, body

    def __enter__(self):
        self.n += 1
        st, body = self._post({
            'jsonrpc': '2.0', 'id': self.n, 'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18',
                       'capabilities': {},
                       'clientInfo': {'name': 'verify', 'version': '1'}}})
        assert st == 200, (st, body)
        self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return self

    def __exit__(self, *a):
        return False

    def rpc(self, method, params=None):
        self.n += 1
        st, body = self._post({'jsonrpc': '2.0', 'id': self.n,
                               'method': method, 'params': params or {}})
        return _sse(body)

    def list_tools(self):
        return [t['name'] for t in self.rpc('tools/list')['result']['tools']]

    def call(self, name, args=None):
        return self.rpc('tools/call', {'name': name, 'arguments': args or {}})


def text_of(resp):
    """Flatten a tools/call result down to its text, error or not."""
    if 'error' in resp:
        return json.dumps(resp['error'])
    c = resp.get('result', {}).get('content') or []
    return ' '.join(x.get('text', '') for x in c if isinstance(x, dict))
