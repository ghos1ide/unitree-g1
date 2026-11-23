"""Durable, sanitized HTTP ingress history. Never sends robot requests."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import uuid
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PATH = Path(__file__).resolve().parents[3] / 'execution/action_runtime_v3/data/http_requests.sqlite3'
SECRET = re.compile(r'token|password|secret|authorization|api.?key|cookie', re.I)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def sanitize(value, key=''):
    if SECRET.search(key):
        return '[redacted]'
    if isinstance(value, dict):
        return {k: sanitize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str) and value.startswith(('http://', 'https://')):
        try:
            url = urlsplit(value)
            return urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path, '', ''))
        except ValueError:
            return '[invalid URL]'
    return value


def body_summary(body):
    try:
        value = sanitize(json.loads(body))
        if len(json.dumps(value).encode()) <= 32768:
            return value
        return {'omitted': 'JSON exceeds 32 KiB', 'bytes': len(body)}
    except (ValueError, UnicodeError, RecursionError):
        return {'omitted': 'Non-JSON body', 'bytes': len(body)}


def diagnose(status, payload):
    if status == 401:
        return 'authorization', 'Authentication rejected'
    if status == 404:
        return ('task_lookup', 'Task ID not found') if isinstance(payload, dict) and payload.get('code') else (
            'routing', 'No matching API route; check method and path')
    if status == 405:
        return 'routing', 'HTTP method is not supported by this route'
    if isinstance(payload, dict):
        message = str(payload.get('message', payload.get('detail', '')))
        if 'No selected recording' in message:
            return 'recording_selection', message
        if payload.get('code') in (2001, 2002, 2003):
            return 'admission', message
        if status >= 400 or payload.get('code', 0) != 0:
            return 'validation' if status == 400 else 'backend_rejected', message
    return ('backend_rejected', 'Backend rejected request') if status >= 400 else ('response_ready', 'HTTP response prepared')


class RequestHistory:
    summarize = staticmethod(body_summary)
    diagnose = staticmethod(diagnose)

    def __init__(self, path=DEFAULT_PATH, recover=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS requests (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    timestamp TEXT NOT NULL, path TEXT NOT NULL, task_id TEXT,
                    http_status INTEGER, stage TEXT NOT NULL, finished INTEGER NOT NULL DEFAULT 0,
                    record TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS request_time ON requests(timestamp);
                CREATE INDEX IF NOT EXISTS request_task ON requests(task_id);
                CREATE TABLE IF NOT EXISTS request_steps (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS step_request ON request_steps(request_id,seq);
            ''')
            if recover:
                for row in db.execute('SELECT id FROM requests WHERE finished=0').fetchall():
                    self._update(db, row['id'], 'interrupted', 'Relay restarted before completion was recorded',
                                 finished=True, outcomeUnknown=True)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self, method, path, peer):
        request_id = uuid.uuid4().hex
        now = stamp()
        record = dict(id=request_id, timestamp=now, method=method, path=path, peer=peer,
                      stage='received', httpStatus=None, acceptance='unknown', source='ingress', finished=False)
        task_match = re.fullmatch(r'/api/v1/tasks/([^/]+)/(?:query|cancel)', path)
        if task_match:
            record['taskId'] = task_match.group(1)
        with self.connect() as db:
            db.execute('INSERT INTO requests(id,timestamp,path,task_id,stage,record) VALUES(?,?,?,?,?,?)',
                       (request_id, now, path, record.get('taskId'), 'received', json.dumps(record)))
            db.execute('INSERT INTO request_steps(request_id,timestamp,stage,message) VALUES(?,?,?,?)',
                       (request_id, now, 'received', 'Request reached port 8090'))
        return request_id

    def _update(self, db, request_id, stage, message, **fields):
        row = db.execute('SELECT record FROM requests WHERE id=?', (request_id,)).fetchone()
        record = json.loads(row['record'])
        record.update(sanitize(fields), stage=stage, message=sanitize(message), updatedAt=stamp())
        db.execute('UPDATE requests SET record=?,task_id=?,http_status=?,stage=?,finished=? WHERE id=?',
                   (json.dumps(record), record.get('taskId'), record.get('httpStatus'), stage,
                    int(record.get('finished', False)), request_id))
        db.execute('INSERT INTO request_steps(request_id,timestamp,stage,message) VALUES(?,?,?,?)',
                   (request_id, record['updatedAt'], stage, sanitize(message)))

    def update(self, request_id, stage, message, **fields):
        with self.connect() as db:
            self._update(db, request_id, stage, message, **fields)

    def import_journal(self, unit):
        access = re.compile(r'\s(\S+) - "([A-Z]+) (\S+) HTTP/[^\"]+" (\d{3})')
        process = subprocess.Popen(['journalctl', '--unit', unit, '--no-pager', '--output=json',
                                    '--grep=/api/|/internal/'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        count = 0
        try:
            with self.connect() as db:
                first = db.execute("SELECT min(timestamp) FROM requests WHERE id NOT LIKE 'journal-%'").fetchone()[0]
                for line in process.stdout:
                    try:
                        row = json.loads(line)
                        match = access.search(row.get('MESSAGE', ''))
                        if not match:
                            continue
                        peer, method, path, status = match.groups()
                        path = path.split('?', 1)[0]
                        now = datetime.fromtimestamp(int(row['__REALTIME_TIMESTAMP']) / 1e6, timezone.utc).isoformat(timespec='microseconds')
                        request_id = 'journal-' + hashlib.sha256(row.get('__CURSOR', line).encode()).hexdigest()
                        record = dict(id=request_id, timestamp=now, method=method, path=path, peer=peer,
                                      httpStatus=int(status), source='journal', stage='historical',
                                      acceptance='unknown', finished=True,
                                      message='Historical access log; request and response bodies were not recorded')
                        # Once structured capture began, journal lines describe the same traffic.
                        if first and now >= first:
                            continue
                        cursor = db.execute('INSERT OR IGNORE INTO requests(id,timestamp,path,http_status,stage,finished,record) '
                                            'VALUES(?,?,?,?,?,1,?)',
                                            (request_id, now, path, int(status), 'historical', json.dumps(record)))
                        count += cursor.rowcount
                    except (ValueError, KeyError, TypeError):
                        continue
            error = process.stderr.read()
            if process.wait() not in (0, 1) or error.strip():
                raise RuntimeError('Journal import incomplete: ' + error[:200])
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
        return count


def read_connection(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def filters(search='', result='all', since=None, until=None, show_health=False, anchor=None):
    clauses, args = [], []
    if not show_health:
        clauses.append("path != '/health'")
    if search:
        clauses.append('instr(lower(record),?)>0')
        args.append(search.lower())
    if result == 'problem':
        clauses.append("(http_status>=400 OR stage IN ('interrupted','transport_error','body_error','backend_rejected','admission','recording_selection','validation'))")
    elif result == 'pending':
        clauses.append('finished=0')
    elif result == 'success':
        clauses.append("http_status<400 AND stage IN ('response_ready','historical')")
    for name, value, operator in [('timestamp', since, '>='), ('timestamp', until, '<='), ('seq', anchor, '<=')]:
        if value is not None:
            clauses.append(name + operator + '?')
            args.append(value)
    return ' AND '.join(clauses) or '1', args


def read_page(path, page=1, page_size=50, **options):
    db = read_connection(path)
    try:
        db.execute('BEGIN')
        maximum = db.execute('SELECT coalesce(max(seq),0) FROM requests').fetchone()[0]
        options['anchor'] = options.get('anchor') or maximum
        where, args = filters(**options)
        total = db.execute('SELECT count(*) FROM requests WHERE ' + where, args).fetchone()[0]
        rows = db.execute('SELECT seq,record FROM requests WHERE ' + where + ' ORDER BY timestamp DESC,seq DESC LIMIT ? OFFSET ?',
                          args + [page_size, (page - 1) * page_size]).fetchall()
        records = []
        for row in rows:
            record = json.loads(row['record'])
            record.pop('requestBody', None)
            record.pop('responseBody', None)
            records.append(record)
        return dict(items=records, total=total, page=page, pageSize=page_size, anchor=options['anchor'],
                    allTotal=db.execute('SELECT count(*) FROM requests').fetchone()[0])
    finally:
        db.close()


def read_detail(path, request_id):
    db = read_connection(path)
    try:
        row = db.execute('SELECT record FROM requests WHERE id=?', (request_id,)).fetchone()
        if row is None:
            return None
        record = json.loads(row['record'])
        record['steps'] = [dict(r) for r in db.execute(
            'SELECT timestamp,stage,message FROM request_steps WHERE request_id=? ORDER BY seq', (request_id,))]
        return record
    finally:
        db.close()


def export_records(path, **options):
    db = read_connection(path)
    try:
        db.execute('BEGIN')
        where, args = filters(**options)
        for row in db.execute('SELECT record FROM requests WHERE ' + where + ' ORDER BY timestamp,seq', args):
            record = json.loads(row['record'])
            record['steps'] = [dict(r) for r in db.execute(
                'SELECT timestamp,stage,message FROM request_steps WHERE request_id=? ORDER BY seq', (record['id'],))]
            yield json.dumps(record, ensure_ascii=False) + '\n'
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_PATH)
    parser.add_argument('--import-journal', required=True)
    args = parser.parse_args()
    print('Imported records:', RequestHistory(args.database).import_journal(args.import_journal))
