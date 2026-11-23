import json
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from deployment import integration_monitor as monitor


def test_access_log():
    def entry(path, status):
        return json.dumps({"MESSAGE": 'INFO:     10.0.0.1:1234 - "POST %s HTTP/1.1" %s' % (path, status),
                           "__REALTIME_TIMESTAMP": "1700000000000000"})
    records = monitor.parse_journal("\n".join([
        "bad json", entry("/health", 200), entry("/api/v1/tasks?token=secret", 401),
        entry("/api/v1/tasks", 200)]))
    assert [r["httpStatus"] for r in records] == [200, 401]
    assert all(r["path"] == "/api/v1/tasks" for r in records)
    assert records[0]["peer"] == "10.0.0.1:1234"
    assert all(r["acceptance"] == "unknown" for r in records)


def test_query_is_not_task_acceptance():
    record = json.dumps({"MESSAGE": 'INFO: 10.0.0.1:1234 - "POST /api/v1/tasks/T1/query HTTP/1.1" 200',
                         "__REALTIME_TIMESTAMP": "1700000000000000"})
    assert monitor.parse_journal(record)[0]["acceptance"] == "not_applicable"


def test_no_matching_journal(monkeypatch):
    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert monitor.read_requests("test.service") == []


def test_database_read_only(tmp_path):
    path = tmp_path / "tasks.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        monitor.read_database(path)
    assert not path.exists()
    with sqlite3.connect(str(path)) as db:
        db.executescript('''CREATE TABLE tasks(id TEXT PRIMARY KEY, request TEXT, event TEXT, status TEXT, cancel_reason TEXT);
        CREATE TABLE outbox(id INTEGER PRIMARY KEY, task_id TEXT, url TEXT, payload TEXT, attempts INTEGER, next_at REAL, delivered INTEGER, last_error TEXT);''')
        db.execute("INSERT INTO tasks VALUES (?,?,?,?,?)", ("T1", '{"apiKey":"secret"}', '{"taskId":"T1"}', "RUNNING", None))
        for index, delivered, attempts in [(1, 1, 1), (2, 0, 0), (3, 0, 4)]:
            db.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)", (index, "T1", "http://user:password@eln/callback?token=secret", '{"taskId":"T1"}', attempts, 0, delivered, None))
    before = path.read_bytes()
    data = monitor.read_database(path)
    assert path.read_bytes() == before
    assert data["taskCounts"] == {"RUNNING": 1}
    assert data["callbackCounts"] == {"total": 3, "delivered": 1, "pending": 1, "exhausted": 1}
    assert data["tasks"][0]["request"]["apiKey"] == "[redacted]"
    assert data["tasks"][0]["acceptance"] == "accepted"
    assert data["callbacks"][0]["url"] == "http://eln/callback"


def test_app_read_only_and_auth(monkeypatch, tmp_path):
    async def collect(*args):
        return {"requests": [], "errors": {}}
    monkeypatch.setattr(monitor, "collect", collect)
    app = monitor.create_app("http://unused", tmp_path / "unused", api_key="test-key")
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/icons/inbox.svg").status_code == 200
        assert client.get("/snapshot").status_code == 401
        assert client.get("/snapshot", headers={"X-API-Key": "test-key"}).json()["errors"] == {}
        assert client.post("/api/v1/tasks", json={}).status_code == 404
        assert client.post("/snapshot").status_code == 405


def test_collection_only_reads_health_and_preserves_source_errors(monkeypatch):
    requests = []

    def respond(request):
        requests.append((request.method, str(request.url)))
        return httpx.Response(200, json={"simulation": False, "token": "private"})

    def unavailable(*args):
        raise PermissionError("unavailable")

    monkeypatch.setattr(monitor, "read_requests", unavailable)
    monkeypatch.setattr(monitor, "read_database", unavailable)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await monitor.collect(client, "http://robot", "unused", "test.service")

    result = asyncio.run(run())
    assert requests == [("GET", "http://robot/health")]
    assert result["health"] == {"simulation": False}
    assert set(result["errors"]) == {"requests", "database"}
    assert result["tasks"] == result["requests"] == result["callbacks"] == []
