import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from robot_gateway.models import TaskEvent


def create_app(database=None):
    @asynccontextmanager
    async def lifespan(app):
        path = database or os.getenv("ELN_DATABASE", "data/eln.sqlite3")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, check_same_thread=False)
        db.execute("""CREATE TABLE IF NOT EXISTS events_v15 (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, status TEXT,
            progress INTEGER, timestamp TEXT, payload TEXT, UNIQUE(task_id,status,progress,timestamp))""")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
            with db:
                for row in db.execute("SELECT payload FROM events ORDER BY id").fetchall():
                    event = json.loads(row[0])
                    db.execute("INSERT OR IGNORE INTO events_v15(task_id,status,progress,timestamp,payload) VALUES(?,?,?,?,?)",
                               (event["taskId"], event["status"], event["progress"], event["timestamp"], row[0]))
        app.state.db = db
        try:
            yield
        finally:
            db.close()

    app = FastAPI(title="ELN Callback Simulator", lifespan=lifespan)

    @app.post("/eln/api/v1/robot-callbacks/task-status")
    async def callback(event: TaskEvent):
        with app.state.db:
            app.state.db.execute("INSERT OR IGNORE INTO events_v15(task_id,status,progress,timestamp,payload) VALUES(?,?,?,?,?)",
                (event.taskId, event.status, event.progress, event.timestamp, event.model_dump_json()))
        logging.getLogger("uvicorn.error").info("%s %s progress=%s", event.taskId, event.status, event.progress)
        return dict(code=0, message="received")

    @app.post("/eln/api/v1/tasks/{task_id}/events")
    async def events(task_id: str):
        rows = app.state.db.execute("SELECT payload FROM events_v15 WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return dict(code=0, events=[json.loads(row[0]) for row in rows])

    return app


app = create_app()
