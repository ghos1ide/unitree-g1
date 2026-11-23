import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from deployment import integration_monitor as monitor
from deployment import request_history as history
from robot_gateway.store import Store


@pytest.fixture
def relay(monkeypatch):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / 'execution/action_runtime_v3/deployment/rail_jetson'))
    from http_relay import create_app
    return create_app


def test_received_is_durable_before_backend_returns(tmp_path, relay):
    audit = history.RequestHistory(tmp_path / 'history.db')

    async def run():
        reached, release = asyncio.Event(), asyncio.Event()

        async def backend(request):
            reached.set()
            await release.wait()
            return httpx.Response(400, json={'code': 1001, 'message': 'No selected recording for: dring_right'})

        app = relay(transport=httpx.MockTransport(backend), history=audit, upstream_api_key='private-key')
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://relay') as client:
                worker = asyncio.create_task(client.post('/api/v1/tasks?token=url-secret', json={
                    'taskId': 'T1', 'taskType': 'NWS_CYCLE', 'apiKey': 'body-secret',
                    'callbackUrl': 'http://u:password@eln.test/cb?secret=hidden', 'nested': {'token': 'nested-secret'}}))
                await reached.wait()
                row = history.read_page(audit.path)['items'][0]
                assert row['stage'] == 'forwarding' and row['httpStatus'] is None
                request_id = row['id']
                release.set()
                result = await worker
                assert result.status_code == 400 and result.headers['X-Request-ID'] == request_id
        record = history.read_detail(audit.path, request_id)
        assert record['stage'] == 'recording_selection'
        assert record['businessCode'] == 1001 and record['taskId'] == 'T1'
        assert record['acceptance'] == 'rejected'
        assert [s['stage'] for s in record['steps']] == [
            'received', 'body_received', 'forwarding', 'upstream_response', 'recording_selection']
        raw = json.dumps(record)
        for secret in ('private-key', 'body-secret', 'url-secret', 'nested-secret', 'password@', 'secret=hidden'):
            assert secret not in raw
        assert record['requestBody']['callbackUrl'] == 'http://eln.test/cb'

    asyncio.run(run())
    assert history.RequestHistory(audit.path, recover=True)
    assert history.read_page(audit.path)['total'] == 1


@pytest.mark.parametrize('status,payload,stage', [
    (401, {'code': 1002, 'message': 'unauthorized'}, 'authorization'),
    (404, {'detail': 'Not Found'}, 'routing'),
    (404, {'code': 1001, 'message': 'task not found'}, 'task_lookup'),
    (400, {'code': 1001, 'message': 'invalid parameters', 'details': [{'field': 'body.count'}]}, 'validation'),
    (409, {'code': 2003, 'message': 'initialize first'}, 'admission'),
])
def test_error_details_are_recorded(tmp_path, relay, status, payload, stage):
    audit = history.RequestHistory(tmp_path / 'history.db')
    app = relay(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload)), history=audit)
    with TestClient(app) as client:
        result = client.post('/api/v1/tasks', json={'taskId': 'test'})
    record = history.read_detail(audit.path, result.headers['X-Request-ID'])
    assert record['stage'] == stage and record['responseBody'] == payload


def test_timeout_and_body_limit_never_retry_or_forward_large_request(tmp_path, relay):
    audit = history.RequestHistory(tmp_path / 'history.db')
    calls = []

    def backend(request):
        calls.append(request)
        raise httpx.ReadTimeout('offline', request=request)

    with TestClient(relay(transport=httpx.MockTransport(backend), history=audit)) as client:
        result = client.post('/api/v1/tasks', json={'taskId': 'timeout'})
        assert result.status_code == 502
        assert history.read_detail(audit.path, result.headers['X-Request-ID'])['stage'] == 'transport_error'
        result = client.post('/api/v1/tasks', content=b'x' * (1024 * 1024 + 1))
        assert result.status_code == 413
        assert history.read_detail(audit.path, result.headers['X-Request-ID'])['stage'] == 'body_error'
    assert len(calls) == 1


def test_query_is_correlated_but_not_classified_as_submission(tmp_path, relay):
    audit = history.RequestHistory(tmp_path / 'history.db')
    app = relay(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        'code': 0, 'taskId': 'T1', 'status': 'ACCEPTED'})), history=audit)
    with TestClient(app) as client:
        result = client.post('/api/v1/tasks/T1/query')
    row = history.read_detail(audit.path, result.headers['X-Request-ID'])
    assert row['taskId'] == 'T1' and row['acceptance'] == 'not_applicable'


def test_audit_failure_does_not_retry_business_request(relay):
    calls = []

    class UnavailableHistory:
        summarize = staticmethod(history.body_summary)

        def start(self, *args):
            raise OSError('disk unavailable')

    def backend(request):
        calls.append(request)
        return httpx.Response(200, json={'code': 0})

    with TestClient(relay(transport=httpx.MockTransport(backend), history=UnavailableHistory())) as client:
        result = client.post('/api/v1/tasks', json={})
        assert result.status_code == 200 and result.headers['X-Request-History'] == 'unavailable'
    assert len(calls) == 1


def test_restart_marks_incomplete_and_history_is_not_capped(tmp_path):
    audit = history.RequestHistory(tmp_path / 'history.db')
    for i in range(205):
        rid = audit.start('POST', '/api/v1/tasks', 'peer:%s' % i)
        audit.update(rid, 'response_ready', 'ok', finished=True, httpStatus=200, taskId='task-%s' % i)
    interrupted = audit.start('POST', '/api/v1/tasks', 'peer')
    first = history.read_page(audit.path)
    audit.start('GET', '/new', 'new-peer')
    second = history.read_page(audit.path, page=2, anchor=first['anchor'])
    assert first['total'] == second['total'] == 206
    assert not set(r['id'] for r in first['items']) & set(r['id'] for r in second['items'])
    assert len(list(history.export_records(audit.path))) == 207
    history.RequestHistory(audit.path, recover=True)
    assert history.read_detail(audit.path, interrupted)['stage'] == 'interrupted'
    assert history.read_page(audit.path, search='task-204')['total'] == 1
    assert history.read_page(audit.path, result='problem')['total'] == 2


def test_history_endpoints_auth_pagination_export_and_task_link(tmp_path, monkeypatch):
    audit = history.RequestHistory(tmp_path / 'history.db')
    task_db = tmp_path / 'tasks.db'
    store = Store(str(task_db), reliable_callbacks=True)
    store.reject(dict(taskId='T1', taskType='NWS_CYCLE', callbackUrl='http://eln.test/cb'), 'Missing recording', 1001)
    store.close()
    rid = audit.start('POST', '/api/v1/tasks', 'peer')
    audit.update(rid, 'recording_selection', 'Missing recording', finished=True, httpStatus=400, taskId='T1')

    async def collect(*args):
        return {'requests': [], 'errors': {}}

    monkeypatch.setattr(monitor, 'collect', collect)
    with TestClient(monitor.create_app('http://unused', task_db, api_key='monitor-key', history_database=audit.path)) as client:
        for path in ('/requests', '/requests/export', '/requests/' + rid):
            assert client.get(path).status_code == 401
        client.headers['X-API-Key'] = 'monitor-key'
        assert client.get('/requests').json()['total'] == 1
        assert client.get('/requests?page=2').json()['items'] == []
        assert client.get('/requests?search=absent').json()['total'] == 0
        assert client.get('/requests/' + rid).json()['task']['state']['message'] == 'Missing recording'
        assert client.get('/requests/unknown').status_code == 404
        assert json.loads(client.get('/requests/export').text)['id'] == rid
        assert client.get('/requests?page_size=1000').status_code == 422


def test_journal_import_is_idempotent_and_does_not_invent_details(tmp_path, monkeypatch):
    line = json.dumps({'__CURSOR': 'unique', '__REALTIME_TIMESTAMP': '1700000000000000',
                       'MESSAGE': 'INFO: 172.18.100.1:1234 - "POST /api/v1/tasks?token=secret HTTP/1.1" 401'})

    class Process:
        def __init__(self, *args, **kwargs):
            self.stdout, self.stderr = io.StringIO(line + '\n'), io.StringIO('')

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(history.subprocess, 'Popen', Process)
    audit = history.RequestHistory(tmp_path / 'history.db')
    assert audit.import_journal('test') == 1
    assert audit.import_journal('test') == 0
    row = history.read_page(audit.path)['items'][0]
    assert row['source'] == 'journal' and row['stage'] == 'historical'
    assert 'responseBody' not in row and 'token' not in row['path']
