"""Explicit editor reads for legacy positive/validation tests.

No TestClient monkeypatch, autouse fixture, or automatic retry. Contract tests
send raw HTTP requests to assert missing/stale revisions and operation IDs.
"""
import time
import uuid


def operation_id():
    return f"{int(time.time() * 1000)}.{uuid.uuid4()}"


def revision_headers(client, headers=None, *, read_path='/api/settings'):
    response = client.get(read_path, headers=headers)
    assert response.status_code == 200, response.status_code
    assert response.headers.get('etag')
    return {**(headers or {}), 'If-Match': response.headers['etag'], 'Idempotency-Key': operation_id()}


async def async_revision_headers(client, headers=None, *, read_path='/api/settings'):
    response = await client.get(read_path, headers=headers)
    assert response.status_code == 200, response.status_code
    assert response.headers.get('etag')
    return {**(headers or {}), 'If-Match': response.headers['etag'], 'Idempotency-Key': operation_id()}
