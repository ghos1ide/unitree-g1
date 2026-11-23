import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from robot_gateway.config import Settings
from robot_gateway.manager import BusinessError, TaskManager
from robot_gateway.models import TaskRequest
from robot_gateway.store import Store


def request(count=5, pre='NONE', mode='FULL_CYCLE', task_id='gear', kind='GEAR_OPERATION'):
    parameters = dict(count=count)
    if kind == 'GEAR_OPERATION':
        parameters.update(preAction=pre, cycleMode=mode)
    return TaskRequest.model_validate(dict(taskId=task_id, taskType=kind, parameters=parameters,
                                          callbackUrl='http://eln.test/callback'))


@pytest.mark.parametrize('count', [1, 5])
@pytest.mark.parametrize('pre', ['NONE', 'EXTEND_DOWNLOCK'])
@pytest.mark.parametrize('mode', ['FULL_CYCLE', 'FINAL_RETRACT_ONLY'])
def test_gear_last_increment_and_success_wait_for_return_to_preparation(tmp_path, monkeypatch, count, pre, mode):
    asyncio.run(scenario(tmp_path, monkeypatch, request(count, pre, mode)))


@pytest.mark.parametrize('outcome', ['FAILED', 'CANCELLED', 'EXCEPTION'])
@pytest.mark.parametrize('count', [1, 5])
def test_exit_failure_never_reports_success_and_blocks_uninitialized_motion(tmp_path, monkeypatch, outcome, count):
    asyncio.run(scenario(tmp_path, monkeypatch, request(count), outcome))


def test_nws_still_waits_for_executor_completion(tmp_path, monkeypatch):
    asyncio.run(scenario(tmp_path, monkeypatch, request(kind='NWS_CYCLE')))


async def scenario(tmp_path, monkeypatch, req, outcome='SUCCEEDED'):
    events = []

    async def post(self, url, **kwargs):
        events.append(json.loads(kwargs['content']))
        return httpx.Response(200, json={'code': 0}, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)

    class Executor:
        def __init__(self):
            self.at_exit = asyncio.Event()
            self.finish_exit = asyncio.Event()

        async def execute(self, request, cancel, report):
            for progress in range(1, request.total + 1):
                await report(progress, 'completed cycle')
            await report(request.total, 'opening hand')
            await report(request.total, 'recorded exit in progress')
            self.at_exit.set()
            await self.finish_exit.wait()
            if outcome == 'EXCEPTION':
                raise RuntimeError('injected exit failure')
            return SimpleNamespace(status=outcome, progress=request.total,
                                   message='recorded exit result', error_code='2004' if outcome == 'FAILED' else None)

        async def close(self):
            pass

    executor = Executor()
    store = Store(str(tmp_path / 'tasks.db'), reliable_callbacks=True)
    manager = TaskManager(Settings(), store, executor)
    await manager.start()
    manager.initialized = True
    try:
        await manager.submit(req)
        await asyncio.wait_for(executor.at_exit.wait(), 2)
        current = manager.query(req.taskId)
        assert current['status'] == 'RUNNING'
        pending_count = req.total - int(req.taskType == 'GEAR_OPERATION')
        assert current['progress'] == pending_count
        assert 'postCompletion' not in current
        for _ in range(100):
            if events and events[-1]['message'] == 'recorded exit in progress':
                break
            await asyncio.sleep(.01)
        assert events[-1]['status'] == 'RUNNING'
        assert events[-1]['message'] == 'recorded exit in progress'
        assert not any(e['status'] == 'SUCCEEDED' for e in events)
        assert {e['progress'] for e in events} == set(range(pending_count + 1))
        assert manager.active == req.taskId
        assert not manager.worker.done()
        with pytest.raises(BusinessError) as error:
            await manager.submit(request(task_id='overlap'))
        assert error.value.code == 2001
        if outcome == 'CANCELLED':
            await manager.request_cancel(req.taskId, 'stop exit')
            assert manager.cancel.is_set()
        executor.finish_exit.set()
        await asyncio.wait_for(manager.worker, 2)
        final = manager.query(req.taskId)
        assert final['status'] == ('FAILED' if outcome == 'EXCEPTION' else outcome)
        assert final['progress'] == (req.total if outcome == 'SUCCEEDED' else pending_count)
        assert manager.active is None
        assert final['timestamp'] != current['timestamp']
        rows = store.db.execute('SELECT payload FROM outbox WHERE task_id=? ORDER BY id', (req.taskId,)).fetchall()
        states = [json.loads(r['payload'])['status'] for r in rows]
        assert states.count('SUCCEEDED') == (1 if outcome == 'SUCCEEDED' else 0)
        assert states[-1] == ('SUCCEEDED' if outcome == 'SUCCEEDED' else 'FAILED')
        if req.taskType == 'GEAR_OPERATION':
            full_count = [json.loads(r['payload']) for r in rows if json.loads(r['payload'])['progress'] == req.total]
            assert len(full_count) == (1 if outcome == 'SUCCEEDED' else 0)
            if full_count:
                assert full_count[0]['status'] == 'SUCCEEDED'
        if outcome != 'SUCCEEDED':
            assert not manager.initialized
            with pytest.raises(BusinessError) as error:
                await manager.submit(request(task_id='after-failure'))
            assert error.value.code == 2003
    finally:
        executor.finish_exit.set()
        await manager.close()


def test_legacy_early_success_record_remains_queryable_after_restart(tmp_path):
    path = str(tmp_path / 'tasks.db')
    store = Store(path, reliable_callbacks=True)
    store.insert(request())
    store.transition('gear', 'SUCCEEDED', 5, 'cycles complete',
                     post_completion=dict(status='RUNNING', message='exit in progress'))
    store.close()
    store = Store(path, reliable_callbacks=True)
    try:
        store.recover()
        row = store.get('gear')
        assert row['status'] == 'SUCCEEDED'
        assert json.loads(row['post_completion'])['status'] == 'FAILED'
        assert json.loads(row['post_completion'])['errorCode'] == '2004'
        assert store.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 2
    finally:
        store.close()


def test_restart_during_return_does_not_add_the_last_count(tmp_path):
    path = str(tmp_path / 'tasks.db')
    store = Store(path, reliable_callbacks=True)
    store.insert(request())
    store.transition('gear', 'RUNNING', 4, 'returning to preparation')
    store.close()
    store = Store(path, reliable_callbacks=True)
    try:
        store.recover()
        event = json.loads(store.get('gear')['event'])
        assert (event['status'], event['progress'], event['errorCode']) == ('FAILED', 4, '2004')
    finally:
        store.close()


def test_old_database_migrates_without_changing_task_event(tmp_path):
    import sqlite3
    path = str(tmp_path / 'old.db')
    req = request()
    event = Store.event(req, 'SUCCEEDED', 5, 'historic result')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, request TEXT, event TEXT, status TEXT, cancel_reason TEXT)')
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,NULL)', ('gear', req.model_dump_json(), json.dumps(event), 'SUCCEEDED'))
    store = Store(path)
    try:
        assert store.get('gear')['post_completion'] is None
        assert json.loads(store.get('gear')['event']) == event
    finally:
        store.close()
