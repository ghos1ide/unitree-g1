"""Read-only ELN integration dashboard; never forwards business requests."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import uvicorn
from deployment.request_history import DEFAULT_PATH, export_records, read_detail, read_page


ROOT = Path(__file__).resolve().parents[3]
ASSETS = Path(__file__).resolve().parents[1] / "web/monitor"
ACCESS = re.compile(r'\s(?P<peer>\S+) - "(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[^\"]+" (?P<status>\d{3})')
SECRET = re.compile(r"token|password|secret|authorization|api.?key", re.I)


def clean(value, key=""):
    if SECRET.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: clean(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        try:
            url = urlsplit(value)
            return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        except ValueError:
            return "[invalid URL]"
    return value


def parse_journal(text):
    events = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
            match = ACCESS.search(row.get("MESSAGE", ""))
            if not match:
                continue
            data = match.groupdict()
            path = data["path"].split("?", 1)[0]
            if not path.startswith(("/api/", "/internal/")):
                continue
            stamp = int(row["__REALTIME_TIMESTAMP"]) / 1000000
            events.append(dict(id=hashlib.sha256(line.encode()).hexdigest()[:20],
                               timestamp=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                               peer=data["peer"], method=data["method"], path=path,
                               httpStatus=int(data["status"]),
                               acceptance="unknown" if data["method"] == "POST" and path in (
                                   "/api/v1/tasks", "/api/v1/robot/initialize") else "not_applicable"))
        except (ValueError, TypeError, KeyError):
            continue
    return list(reversed(events))


def read_requests(unit):
    result = subprocess.run(["journalctl", "--unit", unit, "--no-pager", "--output=json",
                             "--grep=/api/|/internal/", "--lines=200"],
                            capture_output=True, text=True, timeout=4)
    # journalctl --grep returns 1 (without diagnostics) when no entries match.
    if result.returncode and not (result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()):
        raise RuntimeError("Cannot read journal: " + result.stderr.strip()[:200])
    if "permission" in result.stderr.lower() or "not seeing messages" in result.stderr.lower():
        raise RuntimeError("Journal access is restricted; request visibility may be incomplete")
    return parse_journal(result.stdout)


def read_database(path):
    # mode=ro prevents creating a missing database or changing robot task state.
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        outbox_columns = {row['name'] for row in db.execute('PRAGMA table_info(outbox)')}
        retry_forever = 'retry_forever' if 'retry_forever' in outbox_columns else '0'
        counts = {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) AS n FROM tasks GROUP BY status")}
        tasks = []
        for row in db.execute("SELECT rowid,* FROM tasks ORDER BY rowid DESC LIMIT 80"):
            tasks.append(dict(sequence=row["rowid"], request=clean(json.loads(row["request"])),
                              state=clean(json.loads(row["event"])),
                              acceptance="rejected" if 'accepted' in row.keys() and not row['accepted'] else "accepted",
                              cancelRequested=row["cancel_reason"] is not None))
        callbacks = []
        for row in db.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 120"):
            persistent = row['retry_forever'] if 'retry_forever' in row.keys() else False
            delivery = "delivered" if row["delivered"] else (
                "exhausted" if row["attempts"] >= 4 and not persistent else "pending")
            callbacks.append(dict(id=row["id"], url=clean(row["url"]), state=clean(json.loads(row["payload"])),
                                  attempts=row["attempts"], delivery=delivery, error=clean(row["last_error"])))
        cb = db.execute(f"""SELECT count(*) AS total,
            coalesce(sum(delivered=1),0) AS delivered,
            coalesce(sum(delivered=0 AND ({retry_forever}=1 OR attempts<4)),0) AS pending,
            coalesce(sum(delivered=0 AND {retry_forever}=0 AND attempts>=4),0) AS exhausted FROM outbox""").fetchone()
        return dict(tasks=tasks, callbacks=callbacks, taskCounts=counts, callbackCounts=dict(cb))
    finally:
        db.close()


async def collect(client, upstream, database, unit, history_database=None):
    async def health():
        response = await client.get(upstream.rstrip("/") + "/health")
        response.raise_for_status()
        if response.headers.get('X-Request-History') == 'unavailable':
            raise RuntimeError('HTTP request history write failed; check database permissions and disk space')
        value = response.json()
        # Do not expose joint telemetry, ownership internals, or authentication data.
        fields = ("backend", "simulation", "hardwareConnected", "initialized", "activeTaskId",
                  "requiresRecovery", "controllerRunning", "protocolVersion", "completionSemantics")
        return {k: value[k] for k in fields if k in value}
    requests = (run_in_threadpool(read_page, history_database) if history_database else
                run_in_threadpool(read_requests, unit))
    values = await asyncio.gather(health(), requests,
                                  run_in_threadpool(read_database, database), return_exceptions=True)
    snapshot = dict(timestamp=datetime.now(timezone.utc).isoformat(), health=None, requests=[],
                    tasks=[], callbacks=[], taskCounts={}, callbackCounts={}, errors={},
                    sources=dict(api=upstream, journal=unit, database=str(database),
                                 history=str(history_database) if history_database else None))
    for source, value in zip(("health", "requests", "database"), values):
        if isinstance(value, BaseException):
            snapshot["errors"][source] = type(value).__name__ + ": " + str(value)[:200]
        elif source == "database":
            snapshot.update(value)
        elif source == 'requests' and history_database:
            snapshot['requests'] = value.pop('items')
            snapshot['requestPage'] = value
        else:
            snapshot[source] = value
    return snapshot


def related_task(database, task_id):
    db = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not task:
            return None
        callbacks = []
        for row in db.execute('SELECT * FROM outbox WHERE task_id=? ORDER BY id DESC LIMIT 50', (task_id,)):
            persistent = row['retry_forever'] if 'retry_forever' in row.keys() else False
            callbacks.append(dict(state=clean(json.loads(row['payload'])), attempts=row['attempts'],
                                  url=clean(row['url']), error=row['last_error'],
                                  delivery='delivered' if row['delivered'] else
                                  'pending' if persistent or row['attempts'] < 4 else 'exhausted'))
        return dict(state=clean(json.loads(task['event'])), callbacks=callbacks,
                    acceptance='rejected' if 'accepted' in task.keys() and not task['accepted'] else 'accepted')
    finally:
        db.close()


def create_app(upstream, database, unit="eln_interface.service", api_key=None, history_database=DEFAULT_PATH):
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            app.state.snapshot = await collect(client, upstream, database, unit, history_database)
            async def poll():
                while True:
                    await asyncio.sleep(2)
                    app.state.snapshot = await collect(client, upstream, database, unit, history_database)
            worker = asyncio.create_task(poll())
            try:
                yield
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    app = FastAPI(title="ELN Read-only Integration Monitor", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    def authorize(x_api_key: str = Header(default='')):
        from fastapi import HTTPException
        if api_key and not secrets.compare_digest(x_api_key.encode(), api_key.encode()):
            raise HTTPException(401, 'Unauthorized')

    def history_options(search: str = Query(default='', max_length=200),
                        result: Literal['all', 'problem', 'pending', 'success'] = 'all',
                        since: Optional[datetime] = None, until: Optional[datetime] = None,
                        show_health: bool = False, anchor: Optional[int] = Query(default=None, ge=0)):
        return dict(search=search, result=result, show_health=show_health, anchor=anchor,
                    since=since.astimezone(timezone.utc).isoformat(timespec='microseconds') if since else None,
                    until=until.astimezone(timezone.utc).isoformat(timespec='microseconds') if until else None)

    @app.get('/requests', dependencies=[Depends(authorize)])
    async def requests(page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200),
                       options: dict = Depends(history_options)):
        try:
            return await run_in_threadpool(read_page, history_database, page, page_size, **options)
        except (OSError, sqlite3.Error) as error:
            return JSONResponse({'message': 'Request history unavailable: ' + str(error)}, status_code=503)

    @app.get('/requests/export', dependencies=[Depends(authorize)])
    async def export(options: dict = Depends(history_options)):
        try:
            page = await run_in_threadpool(read_page, history_database, 1, 1, **options)
            options['anchor'] = page['anchor']
        except (OSError, sqlite3.Error) as error:
            return JSONResponse({'message': str(error)}, status_code=503)
        return StreamingResponse(export_records(history_database, **options), media_type='application/x-ndjson',
                                 headers={'Content-Disposition': 'attachment; filename="eln-request-history.ndjson"'})

    @app.get('/requests/{request_id}', dependencies=[Depends(authorize)])
    async def request_detail(request_id: str):
        try:
            record = await run_in_threadpool(read_detail, history_database, request_id)
            if record is None:
                return JSONResponse({'message': 'Request record not found'}, status_code=404)
            if record.get('taskId'):
                try:
                    record['task'] = await run_in_threadpool(related_task, database, record['taskId'])
                except (OSError, sqlite3.Error) as error:
                    record['taskError'] = str(error)
            return record
        except (OSError, sqlite3.Error) as error:
            return JSONResponse({'message': str(error)}, status_code=503)

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/snapshot")
    async def snapshot(x_api_key: str = Header(default="")):
        if api_key and not secrets.compare_digest(x_api_key.encode(), api_key.encode()):
            return JSONResponse({"message": "Unauthorized"}, status_code=401)
        return JSONResponse(app.state.snapshot, headers={"Cache-Control": "no-store"})

    return app


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--upstream", default="http://127.0.0.1:8090")
    parser.add_argument("--unit", default="eln_interface.service")
    parser.add_argument("--database", type=Path, default=ROOT / "execution/action_runtime_v3/data/production.sqlite3")
    parser.add_argument('--history-database', type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    uvicorn.run(create_app(args.upstream, args.database, args.unit, os.getenv("MONITOR_API_KEY"), args.history_database),
                host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
