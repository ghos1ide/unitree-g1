import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import TERMINAL, TaskRequest


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    """State and callback outbox commit together; actions are never replayed."""

    def __init__(self, path, reliable_callbacks=False):
        self.reliable_callbacks = reliable_callbacks
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, request TEXT NOT NULL, event TEXT NOT NULL,
                status TEXT NOT NULL, cancel_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                url TEXT NOT NULL, payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0,
                delivered INTEGER NOT NULL DEFAULT 0, last_error TEXT
            );
        """)
        if 'retry_forever' not in {row['name'] for row in self.db.execute('PRAGMA table_info(outbox)')}:
            self.db.execute('ALTER TABLE outbox ADD COLUMN retry_forever INTEGER NOT NULL DEFAULT 0')
        if 'accepted' not in {row['name'] for row in self.db.execute('PRAGMA table_info(tasks)')}:
            self.db.execute('ALTER TABLE tasks ADD COLUMN accepted INTEGER NOT NULL DEFAULT 1')
        if 'post_completion' not in {row['name'] for row in self.db.execute('PRAGMA table_info(tasks)')}:
            self.db.execute('ALTER TABLE tasks ADD COLUMN post_completion TEXT')
        if reliable_callbacks:
            with self.db:
                for row in self.db.execute('SELECT id,payload FROM outbox WHERE delivered=0').fetchall():
                    event = json.loads(row['payload'])
                    if event['status'] in TERMINAL:
                        event = self.final_event(event)
                        self.db.execute('UPDATE outbox SET payload=?,retry_forever=1 WHERE id=?',
                                        (json.dumps(event), row['id']))

    def get(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def insert(self, request: TaskRequest):
        event = self.event(request, "ACCEPTED", 0, "accepted")
        with self.db:
            self.db.execute("INSERT INTO tasks(id,request,event,status) VALUES(?,?,?,?)", (
                request.taskId, request.model_dump_json(), json.dumps(event), "ACCEPTED"))
            self._enqueue(str(request.callbackUrl), event)
        return event

    @staticmethod
    def event(request, status, progress, message, error_code=None):
        return dict(taskId=request.taskId, taskType=request.taskType, status=status,
                    progress=progress, message=message, timestamp=timestamp(), errorCode=error_code)

    def _enqueue(self, callback_url, event):
        if self.reliable_callbacks:
            event = self.final_event(event)
        self.db.execute("INSERT INTO outbox(task_id,url,payload,retry_forever) VALUES(?,?,?,?)", (
            event["taskId"], callback_url, json.dumps(event),
            int(self.reliable_callbacks and event['status'] in TERMINAL)))

    @staticmethod
    def final_event(event):
        if event['status'] == 'CANCELLED':
            return dict(event, status='FAILED', errorCode=event.get('errorCode') or 'CANCELLED')
        return event

    def reject(self, request, message, error_code):
        # Reserve rejected IDs too: a retry must not turn a reported failure into motion.
        with self.db:
            existing = self.get(request['taskId'])
            if existing:
                return json.loads(existing['event']), False
            event = dict(taskId=request['taskId'], taskType=request['taskType'], status='FAILED',
                         progress=0, message=message, timestamp=timestamp(), errorCode=str(error_code))
            self.db.execute('INSERT INTO tasks(id,request,event,status,accepted) VALUES(?,?,?,?,0)', (
                request['taskId'], json.dumps(request), json.dumps(event), 'FAILED'))
            self._enqueue(request['callbackUrl'], event)
        return event, True

    def transition(self, task_id, status, progress, message, error_code=None, post_completion=None):
        row = self.get(task_id)
        old = json.loads(row["event"])
        if old["status"] in TERMINAL:
            return old
        # Historical V1.0 records remain queryable and recoverable after the upgrade.
        request = json.loads(row["request"])
        total = request["parameters"].get("count", 1)
        if not old["progress"] <= progress <= total:
            raise ValueError("invalid controller progress")
        if status == "SUCCEEDED" and progress != total:
            raise ValueError("success requires all operations to complete")
        event = dict(old, status=status, progress=progress, message=message,
                     errorCode=error_code, timestamp=timestamp())
        with self.db:
            self.db.execute("UPDATE tasks SET event=?,status=?,post_completion=COALESCE(?,post_completion) WHERE id=?", (
                json.dumps(event), status,
                json.dumps(dict(post_completion, timestamp=event['timestamp'])) if post_completion is not None else None,
                task_id))
            self._enqueue(request["callbackUrl"], event)
        return event

    def update_post_completion(self, task_id, status, message, error_code=None):
        state = dict(status=status, message=message, errorCode=error_code, timestamp=timestamp())
        with self.db:
            self.db.execute('UPDATE tasks SET post_completion=? WHERE id=?', (json.dumps(state), task_id))

    def request_cancel(self, task_id, reason):
        with self.db:
            self.db.execute("UPDATE tasks SET cancel_reason=? WHERE id=?", (reason, task_id))

    def recover(self):
        for row in self.db.execute('SELECT id,post_completion FROM tasks WHERE post_completion IS NOT NULL').fetchall():
            if json.loads(row['post_completion'])['status'] == 'RUNNING':
                self.update_post_completion(row['id'], 'FAILED',
                                            'Gateway restarted during recorded exit; recovery required', '2004')
        rows = self.db.execute("SELECT id,event FROM tasks WHERE status IN ('ACCEPTED','RUNNING')").fetchall()
        for row in rows:
            self.transition(row["id"], "FAILED", json.loads(row["event"])["progress"],
                            "Gateway restarted; controller outcome is unknown", "2004")

    def pending_callback(self, now):
        # Preserve event order within a task without blocking unrelated tasks.
        return self.db.execute("""
            SELECT o.* FROM outbox o WHERE delivered=0 AND (retry_forever=1 OR attempts<4) AND next_at<=?
            AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.task_id=o.task_id
                AND p.id<o.id AND p.delivered=0 AND (p.retry_forever=1 OR p.attempts<4))
            ORDER BY o.id LIMIT 1
        """, (now,)).fetchone()

    def callback_result(self, row, success, error, next_at):
        with self.db:
            self.db.execute("UPDATE outbox SET attempts=attempts+1,delivered=?,last_error=?,next_at=? WHERE id=?",
                            (int(success), error, next_at, row["id"]))

    def callback_stats(self, task_id):
        rows = self.db.execute("SELECT delivered,attempts,retry_forever FROM outbox WHERE task_id=?", (task_id,)).fetchall()
        return dict(delivered=sum(r["delivered"] for r in rows),
                    pending=sum(not r["delivered"] and (r['retry_forever'] or r["attempts"] < 4) for r in rows),
                    exhausted=sum(not r["delivered"] and not r['retry_forever'] and r["attempts"] >= 4 for r in rows))

    def close(self):
        self.db.close()
