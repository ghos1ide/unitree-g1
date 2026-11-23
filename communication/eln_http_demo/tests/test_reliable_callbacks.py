import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from deployment.integration_monitor import read_database
from robot_gateway.app import create_app
from robot_gateway.config import Settings
from robot_gateway.models import TaskRequest
from robot_gateway.store import Store

URL = 'http://eln.test/callback'


def task(task_id='task', count=1):
    return dict(taskId=task_id, taskType='NWS_CYCLE', callbackUrl=URL, parameters={'count': count})


def wait_until(check):
    deadline = time.monotonic() + 5
    while not check():
        assert time.monotonic() < deadline
        time.sleep(.01)


@pytest.fixture
def events(monkeypatch):
    received = []

    async def post(self, url, **kwargs):
        received.append(json.loads(kwargs['content']))
        return httpx.Response(200, json={'code': 0}, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    return received


def test_four_states_success_cancel_and_duplicate(tmp_path, events):
    settings = Settings(database=str(tmp_path / 'tasks.db'), reliable_callbacks=True, mock_step_seconds=.001)
    with TestClient(create_app(settings)) as client:
        client.post('/api/v1/robot/initialize', json=dict(taskId='init', callbackUrl=URL))
        wait_until(lambda: any(e['status'] == 'SUCCEEDED' for e in events))
        assert {e['status'] for e in events} == {'ACCEPTED', 'RUNNING', 'SUCCEEDED'}
        response = client.post('/api/v1/robot/initialize', json=dict(taskId='init', callbackUrl=URL))
        assert response.json()['duplicate'] is True
        assert response.json()['status'] == 'SUCCEEDED'
        assert response.json()['callbackScheduled'] is False
        assert not any(e['taskId'] == 'init' and e['status'] == 'FAILED' for e in events)
        assert client.post('/api/v1/tasks', json=task('cancel', 10000)).json()['code'] == 0
        client.post('/api/v1/tasks/cancel/cancel', json={})
        wait_until(lambda: any(e['taskId'] == 'cancel' and e['status'] == 'FAILED' for e in events))
        assert all(e['status'] in {'ACCEPTED', 'RUNNING', 'SUCCEEDED', 'FAILED'} for e in events)
        cancelled = next(e for e in events if e['taskId'] == 'cancel' and e['status'] == 'FAILED')
        assert cancelled['errorCode'] == 'CANCELLED'


@pytest.mark.parametrize('count,code', [(0, 1001), (1, 2003)])
def test_rejected_requests_have_durable_final_result(tmp_path, events, count, code):
    path = str(tmp_path / 'tasks.db')
    with TestClient(create_app(Settings(database=path, reliable_callbacks=True))) as client:
        result = client.post('/api/v1/tasks', json=task(count=count)).json()
        assert result['code'] == code
        assert result['status'] == 'FAILED' and result['callbackScheduled'] is True
        wait_until(lambda: len(events) == 1)
        assert events[0]['status'] == 'FAILED' and events[0]['errorCode'] == str(code)
        query = client.post('/api/v1/tasks/task/query').json()
        assert query['status'] == 'FAILED'
        duplicate = client.post('/api/v1/tasks', json=task(count=count)).json()
        assert duplicate['duplicate'] is True and duplicate['callbackScheduled'] is False
        assert read_database(path)['tasks'][0]['acceptance'] == 'rejected'


def test_missing_callback_or_bad_json_returns_failure_without_callback(tmp_path, events):
    with TestClient(create_app(Settings(database=str(tmp_path / 'tasks.db'), reliable_callbacks=True))) as client:
        for body in ({}, dict(task(), callbackUrl='invalid'), dict(task(), callbackUrl='http://u:p@eln.test')):
            result = client.post('/api/v1/tasks', json=body).json()
            assert result['status'] == 'FAILED' and result['callbackScheduled'] is False
        response = client.post('/api/v1/tasks', content='{', headers={'Content-Type': 'application/json'})
        assert response.status_code == 400 and response.json()['status'] == 'FAILED'
        assert events == []


def test_retry_past_four_attempts_preserves_payload_and_does_not_execute(tmp_path, monkeypatch):
    attempts = []

    async def post(self, url, **kwargs):
        attempts.append(kwargs['content'])
        if len(attempts) < 6:
            raise httpx.ConnectError('offline')
        return httpx.Response(200, json={'code': 0}, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    settings = Settings(database=str(tmp_path / 'tasks.db'), reliable_callbacks=True, callback_retry_seconds=.001)
    with TestClient(create_app(settings)) as client:
        client.post('/api/v1/tasks', json=task())
        wait_until(lambda: client.post('/api/v1/tasks/task/query').json()['callbackDelivery']['delivered'] == 1)
        assert len(attempts) == 6 and len(set(attempts)) == 1
        assert client.get('/health').json()['activeTaskId'] is None
        assert client.post('/api/v1/tasks/task/query').json()['callbackDelivery']['exhausted'] == 0


def test_busy_and_duplicate_active_task_do_not_overwrite_original(tmp_path, events):
    settings = Settings(database=str(tmp_path / 'tasks.db'), reliable_callbacks=True, mock_step_seconds=.001)
    with TestClient(create_app(settings)) as client:
        client.post('/api/v1/robot/initialize', json=dict(taskId='init', callbackUrl=URL))
        wait_until(lambda: client.get('/health').json()['initialized'])
        assert client.post('/api/v1/tasks', json=task('active', 10000)).json()['code'] == 0
        duplicate = client.post('/api/v1/tasks', json=task('active', 10000)).json()
        assert duplicate['duplicate'] and duplicate['status'] in {'ACCEPTED', 'RUNNING'}
        busy = client.post('/api/v1/tasks', json=task('busy')).json()
        assert busy['code'] == 2001 and busy['callbackScheduled']
        wait_until(lambda: any(e['taskId'] == 'busy' for e in events))
        assert next(e for e in events if e['taskId'] == 'busy')['status'] == 'FAILED'
        assert not any(e['taskId'] == 'active' and e['status'] == 'FAILED' for e in events)
        client.post('/api/v1/tasks/active/cancel', json={})


def test_execution_exception_has_failed_final_callback(tmp_path, events):
    class BrokenExecutor:
        async def execute(self, *args):
            raise RuntimeError('offline test failure')

        async def close(self):
            pass

    settings = Settings(database=str(tmp_path / 'tasks.db'), reliable_callbacks=True)
    with TestClient(create_app(settings, BrokenExecutor())) as client:
        result = client.post('/api/v1/robot/initialize', json=dict(taskId='broken', callbackUrl=URL))
        assert result.json()['code'] == 0
        wait_until(lambda: any(e['status'] == 'FAILED' for e in events))
        assert [e['status'] for e in events] == ['ACCEPTED', 'RUNNING', 'FAILED']
        assert events[-1]['errorCode'] == '2004'


def test_old_outbox_migration_restart_and_pending_monitor(tmp_path):
    path = str(tmp_path / 'tasks.db')
    store = Store(path)
    store.insert(TaskRequest.model_validate(task()))
    store.transition('task', 'RUNNING', 0, 'running')
    store.transition('task', 'CANCELLED', 0, 'cancelled')
    with store.db:
        store.db.execute('UPDATE outbox SET attempts=4')
    store.close()
    store = Store(path, reliable_callbacks=True)
    row = store.pending_callback(time.time())
    assert json.loads(row['payload'])['status'] == 'FAILED'
    assert row['retry_forever'] == 1
    store.callback_result(row, False, 'offline', 0)
    store.close()
    store = Store(path, reliable_callbacks=True)
    assert store.pending_callback(time.time())['attempts'] == 5
    assert store.callback_stats('task')['pending'] == 1
    store.insert(TaskRequest.model_validate(task('interrupted')))
    store.recover()
    assert json.loads(store.get('interrupted')['event'])['status'] == 'FAILED'
    store.close()
    data = read_database(path)
    assert next(c for c in data['callbacks'] if c['state']['taskId'] == 'task' and
                c['state']['status'] == 'FAILED')['delivery'] == 'pending'


def test_production_missing_recording_and_unexpected_error_callback(tmp_path, monkeypatch, events):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / 'execution/action_runtime_v3/deployment/rail_jetson'))
    import production_api

    release = dict(sha256='offline-test', payload=dict(release_kind='full', version='test',
                                                     actions={'dring_left': {}}, mapping={}))

    class Runtime:
        def __init__(self, *args):
            self.last_state = dict(releaseSha256='offline-test', simulation=True, recoveryRequired=False,
                                   native={})

        async def start(self):
            pass

        async def rpc(self, *args):
            return self.last_state

        async def close(self):
            pass

    monkeypatch.setattr(production_api, 'NativeRuntime', Runtime)
    monkeypatch.setattr(production_api, 'load_release', lambda _: release)
    # This admission test uses an intentionally incomplete release, including
    # at the startup validation boundary introduced by PinnedRelease.
    import release_validation_cache
    monkeypatch.setattr(release_validation_cache, 'validate', lambda value: value)
    app = production_api.create_app(root / 'communication/eln_http_demo', 'unused', tmp_path / 'tasks.db',
                                    'offline-token', 'offline-key', allow_offline=True)
    with TestClient(app) as client:
        client.headers['X-API-Key'] = 'offline-key'
        result = client.post('/api/v1/tasks', json=task()).json()
        assert result['code'] == 1001 and 'dring_right' in result['message']
        assert result['callbackScheduled'] is True
        wait_until(lambda: len(events) == 1)
        assert events[0]['status'] == 'FAILED' and 'dring_right' in events[0]['message']

        def broken(*args):
            raise RuntimeError('unexpected validation failure')

        monkeypatch.setattr(app.state.manager.executor, 'required_actions', broken)
        result = client.post('/api/v1/tasks', json=task('unexpected'))
        assert result.status_code == 500 and result.json()['callbackScheduled'] is True
        wait_until(lambda: len(events) == 2)
        assert events[-1]['status'] == 'FAILED' and events[-1]['errorCode'] == '9000'
