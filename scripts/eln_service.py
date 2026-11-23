#!/usr/bin/env python3
"""ELN V1 HTTP gateway. Simulation only until a verified executor is supplied."""

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

from fastapi import Depends, FastAPI, Request as HttpRequest
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException
from typing import Any, Dict, Literal, Optional
import uvicorn


FINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
TYPES = {"ROBOT_INITIALIZE", "PEDAL_ROTATE", "MLG_WOW", "GEAR_OPERATION"}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SimulationExecutor:
    """No hardware access. Each completed wait represents one simulated action."""

    def __init__(self, delay=0.2):
        self.delay = delay

    def check(self, task_type, parameters):
        pass

    def step(self, task_type, parameters, cancel):
        return not cancel.wait(self.delay)


class DisabledExecutor:
    def check(self, task_type, parameters):
        raise ApiError(2002, "Real action executor and safety interlocks are not configured")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Service:
    def __init__(self, database, executor, callback_url="", allowed_callbacks=()):
        self.executor = executor
        self.callback_url = callback_url
        self.allowed_callbacks = set(allowed_callbacks) | ({callback_url} if callback_url else set())
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.cancel = threading.Event()
        self.initialized = False
        self.active = None
        self.worker = None
        self.database_lock = open(str(Path(database).resolve()) + ".lock", "a")
        try:
            fcntl.flock(self.database_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.database_lock.close()
            raise RuntimeError("Another ELN service owns this database") from None
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, request TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS callbacks (
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, url TEXT NOT NULL,
                payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0);
        """)
        # Execution is never resumed after a process restart: its physical outcome is unknown.
        with self.db:
            for task_id, request, state in self.db.execute("SELECT id, request, state FROM tasks").fetchall():
                state = json.loads(state)
                if state["status"] not in FINAL:
                    self._transition(task_id, json.loads(request), "FAILED", state["progress"],
                                     "Service restarted; execution outcome requires inspection", "9000")
        self.callback_worker = threading.Thread(target=self._callbacks, daemon=True)
        self.callback_worker.start()

    def _transition(self, task_id, request, status, progress, message, error=None):
        state = dict(taskId=task_id, taskType=request["taskType"], status=status,
                     progress=progress, message=message,
                     timestamp=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                     errorCode=error)
        payload = json.dumps(state, ensure_ascii=False)
        self.db.execute("UPDATE tasks SET state=? WHERE id=?", (payload, task_id))
        self.db.execute("INSERT INTO callbacks(task_id,url,payload) VALUES (?,?,?)",
                        (task_id, request["callbackUrl"], payload))
        return state

    def _validate(self, body, initialize):
        if not isinstance(body, dict):
            raise ApiError(1001, "JSON body must be an object")
        request = dict(body)
        if initialize:
            if "taskType" in request and request["taskType"] != "ROBOT_INITIALIZE":
                raise ApiError(1001, "Initialization requires ROBOT_INITIALIZE")
            request["taskType"] = "ROBOT_INITIALIZE"
            request.setdefault("taskId", "INIT-" + uuid.uuid4().hex)
            request.setdefault("parameters", {})
        task_id = request.get("taskId")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", task_id):
            raise ApiError(1001, "taskId must contain 1-128 letters, digits, dots, underscores or hyphens")
        kind, params = request.get("taskType"), request.get("parameters")
        if not isinstance(kind, str) or kind not in TYPES or not isinstance(params, dict):
            raise ApiError(1001, "Invalid taskType or parameters")
        if kind == "PEDAL_ROTATE":
            if params.get("direction") not in ("LEFT", "RIGHT") or type(params.get("count")) is not int or not 1 <= params["count"] <= 10000:
                raise ApiError(1001, "direction must be LEFT/RIGHT; count must be an integer in 1..10000")
        if kind == "MLG_WOW" and params.get("wowState") not in ("ON", "OFF"):
            raise ApiError(1001, "wowState must be ON/OFF")
        if kind == "GEAR_OPERATION" and params.get("action") not in ("RETRACT", "EXTEND"):
            raise ApiError(1001, "action must be RETRACT/EXTEND")
        url = request.setdefault("callbackUrl", self.callback_url)
        if not isinstance(url, str) or not url or url not in self.allowed_callbacks:
            raise ApiError(1001, "callbackUrl must match a configured callback address")
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password and not parsed.fragment
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ApiError(1001, "Invalid HTTP/HTTPS callback URL")
        return request

    def submit(self, body, initialize=False):
        request = self._validate(body, initialize)
        task_id, kind = request["taskId"], request["taskType"]
        with self.lock:
            if self.db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise ApiError(1003, "Task ID already exists")
            if self.active is not None:
                raise ApiError(2001, "Robot is busy")
            if self.stop.is_set():
                raise ApiError(2002, "Service is shutting down")
            if kind != "ROBOT_INITIALIZE" and not self.initialized:
                raise ApiError(2002, "Robot initialization is required")
            self.executor.check(kind, request["parameters"])
            with self.db:
                self.db.execute("INSERT INTO tasks VALUES (?,?,?)", (task_id, json.dumps(request), "{}"))
                self._transition(task_id, request, "ACCEPTED", 0, "accepted")
            self.active = task_id
            if kind == "ROBOT_INITIALIZE":
                self.initialized = False
            self.cancel.clear()
            self.worker = threading.Thread(target=self._execute, args=(request,), daemon=True)
            self.worker.start()
        return dict(code=0, message="accepted", taskId=task_id, status="ACCEPTED")

    def query(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ApiError(2004, "Task not found")
            return dict(json.loads(row[0]), code=0)

    def cancel_task(self, task_id, body):
        if not isinstance(body, dict) or not isinstance(body.get("reason", ""), str):
            raise ApiError(1001, "reason must be a string")
        with self.lock:
            state = self.query(task_id)
            if state["status"] not in FINAL:
                self.cancel.set()
                state["message"] = "Cancellation requested; waiting for controlled stop"
            return state

    def _execute(self, request):
        task_id, kind, params = request["taskId"], request["taskType"], request["parameters"]
        progress = 0
        try:
            with self.lock, self.db:
                self._transition(task_id, request, "RUNNING", 0, "running")
            total = params["count"] if kind == "PEDAL_ROTATE" else 1
            while progress < total:
                if self.cancel.is_set():
                    break
                self.executor.check(kind, params)
                if not self.executor.step(kind, params, self.cancel):
                    if not self.cancel.is_set():
                        raise RuntimeError("Executor stopped without a cancellation request")
                    break
                progress += 1
                if progress < total:
                    with self.lock, self.db:
                        self._transition(task_id, request, "RUNNING", progress, f"completed {progress}/{total}")
            with self.lock, self.db:
                status = "SUCCEEDED" if progress == total else "CANCELLED"
                self._transition(task_id, request, status, progress, status.lower())
                if kind == "ROBOT_INITIALIZE":
                    self.initialized = status == "SUCCEEDED"
        except Exception as exc:
            logging.exception("Task execution failed: %s", task_id)
            with self.lock, self.db:
                self.initialized = False
                self._transition(task_id, request, "FAILED", progress, str(exc),
                                 str(exc.code) if isinstance(exc, ApiError) else "9000")
        finally:
            with self.lock:
                self.active = None

    def _callbacks(self):
        opener = build_opener(NoRedirect())
        while not self.stop.is_set():
            with self.lock:
                row = self.db.execute("""
                    SELECT c.id,c.url,c.payload,c.attempts FROM callbacks c
                    WHERE c.delivered=0 AND c.attempts<4 AND c.due<=?
                    AND NOT EXISTS (SELECT 1 FROM callbacks p WHERE p.task_id=c.task_id
                        AND p.id<c.id AND p.delivered=0 AND p.attempts<4)
                    ORDER BY c.id LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                self.stop.wait(0.05)
                continue
            event_id, url, payload, attempts = row
            delivered = False
            try:
                if url not in self.allowed_callbacks:
                    raise ValueError("Stored callback address is no longer allowed")
                request = Request(url, payload.encode("utf-8"), {"Content-Type": "application/json"}, method="POST")
                with opener.open(request, timeout=3) as response:
                    raw = response.read(65537)
                    if len(raw) > 65536:
                        raise ValueError("Callback response too large")
                    result = json.loads(raw)
                    delivered = 200 <= response.status < 300 and isinstance(result, dict) and type(result.get("code")) is int and result["code"] == 0
            except Exception as exc:
                logging.warning("Callback %s failed: %s", event_id, exc)
            with self.lock, self.db:
                self.db.execute("UPDATE callbacks SET attempts=?,due=?,delivered=? WHERE id=?",
                                (attempts + 1, time.time() + 2 ** attempts, int(delivered), event_id))
            if not delivered and attempts == 3:
                logging.error("Callback %s exhausted retries; retained in database", event_id)

    def close(self):
        with self.lock:
            self.stop.set()
            self.cancel.set()
        if self.worker:
            self.worker.join()
        self.callback_worker.join()
        self.db.close()
        self.database_lock.close()


class TaskRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    taskId: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    taskType: Literal["ROBOT_INITIALIZE", "PEDAL_ROTATE", "MLG_WOW", "GEAR_OPERATION"]
    callbackUrl: Optional[str] = None
    parameters: Dict[str, Any] = Field(json_schema_extra={
        "examples": [{"direction": "LEFT", "count": 20}, {"wowState": "ON"}, {"action": "RETRACT"}]})


class InitializeRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    taskId: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    callbackUrl: Optional[str] = None
    taskType: Literal["ROBOT_INITIALIZE"] = "ROBOT_INITIALIZE"
    parameters: Dict[str, Any] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    reason: str = ""


def create_app(service, token=""):
    app = FastAPI(title="ELN Robot Task API", version="1.0.0",
                  description="ELN task communication. Simulation does not operate hardware.",
                  dependencies=[Depends(HTTPBearer(auto_error=False))])

    def error(code, message, status=400):
        return JSONResponse(status_code=status, content=dict(code=code, message=message))

    @app.middleware("http")
    async def validate_request(request, call_next):
        if request.url.path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
            return await call_next(request)
        if token and not hmac.compare_digest(request.headers.get("authorization", "").encode(),
                                             ("Bearer " + token).encode()):
            return error(1002, "Unauthorized", 401)
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return error(1001, "Content-Type must be application/json")
            try:
                if request.headers.get("transfer-encoding") or len(request.headers.getlist("content-length")) != 1:
                    return error(1001, "One Content-Length header is required")
                size = int(request.headers["content-length"])
                if not 0 < size <= 65536:
                    return error(1001, "Body size must be 1..65536 bytes")
                raw = await asyncio.wait_for(request.body(), timeout=5)
                if len(raw) != size:
                    return error(1001, "Body length mismatch")
                json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON number")))
            except (ValueError, UnicodeError, asyncio.TimeoutError):
                return error(1001, "Invalid JSON request")
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def api_error(request: HttpRequest, exc: ApiError):
        return error(exc.code, str(exc), 401 if exc.code == 1002 else 400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: HttpRequest, exc: RequestValidationError):
        return error(1001, "Invalid request fields")

    @app.exception_handler(HTTPException)
    async def http_error(request: HttpRequest, exc: HTTPException):
        return error(2004 if exc.status_code == 404 else 1001, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error(request: HttpRequest, exc: Exception):
        logging.error("Request failed", exc_info=(type(exc), exc, exc.__traceback__))
        return error(9000, "Internal service error", 500)

    @app.post("/api/v1/tasks", summary="Submit a robot task")
    def submit(body: TaskRequest):
        return service.submit(body.model_dump(exclude_unset=True))

    @app.post("/api/v1/robot/initialize", summary="Initialize the robot")
    def initialize(body: InitializeRequest):
        return service.submit(body.model_dump(exclude_unset=True), initialize=True)

    @app.post("/api/v1/tasks/{task_id}/query", summary="Query task state")
    def query(task_id: str, body: Dict[str, Any]):
        return service.query(task_id)

    @app.post("/api/v1/tasks/{task_id}/cancel", summary="Request controlled stop")
    def cancel(task_id: str, body: CancelRequest):
        return service.cancel_task(task_id, body.model_dump())

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--database", default=str(Path(__file__).resolve().parents[1] / "data/eln/tasks.sqlite3"))
    parser.add_argument("--callback-url", default="")
    parser.add_argument("--allow-callback-url", action="append", default=[])
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--simulation-delay", type=float, default=0.2)
    args = parser.parse_args()
    token = os.environ.get("ELN_API_TOKEN", "")
    if args.bind not in ("127.0.0.1", "::1", "localhost") and not token:
        parser.error("Non-loopback binding requires ELN_API_TOKEN")
    if not math.isfinite(args.simulation_delay) or args.simulation_delay <= 0:
        parser.error("--simulation-delay must be finite and positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    executor = SimulationExecutor(args.simulation_delay) if args.simulate else DisabledExecutor()
    service = Service(args.database, executor, args.callback_url, args.allow_callback_url)
    logging.info("ELN service http://%s:%s mode=%s", args.bind, args.port,
                 "SIMULATION (no hardware)" if args.simulate else "DISABLED (no action executor)")
    try:
        uvicorn.run(create_app(service, token), host=args.bind, port=args.port, workers=1)
    finally:
        service.close()


if __name__ == "__main__":
    main()
