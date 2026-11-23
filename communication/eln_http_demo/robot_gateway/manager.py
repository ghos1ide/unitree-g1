import asyncio
import json
import logging
import time

import httpx

from .models import TERMINAL

logger = logging.getLogger(__name__)


class BusinessError(Exception):
    def __init__(self, code, message, http_status=409):
        self.code, self.message, self.http_status = code, message, http_status


class TaskManager:
    def __init__(self, settings, store, executor):
        self.settings, self.store, self.executor = settings, store, executor
        self.initialized = False
        self.active = None
        self.cancel = None
        self.worker = None
        self.callback_worker = None
        self.lock = asyncio.Lock()
        self.stopping = False

    async def start(self):
        self.store.recover()
        self.client = httpx.AsyncClient(timeout=self.settings.callback_timeout, trust_env=False)
        self.callback_worker = asyncio.create_task(self.deliver_callbacks())

    async def submit(self, request):
        async with self.lock:
            if self.store.get(request.taskId):
                raise BusinessError(1003, "taskId already exists")
            if self.stopping or self.active:
                raise BusinessError(2001, "robot busy")
            if request.taskType != "ROBOT_INITIALIZE" and not self.initialized:
                raise BusinessError(2003, "robot must initialize successfully first")
            event = self.store.insert(request)
            self.active = request.taskId
            self.cancel = asyncio.Event()
            if request.taskType == "ROBOT_INITIALIZE":
                self.initialized = False
            self.worker = asyncio.create_task(self.execute(request, self.cancel))
            return dict(code=0, message="accepted", taskId=request.taskId, status=event["status"])

    def query(self, task_id):
        row = self.store.get(task_id)
        if not row:
            raise BusinessError(1001, "task not found", 404)
        result = dict(code=0, **json.loads(row["event"]),
                      cancelRequested=row["cancel_reason"] is not None,
                      callbackDelivery=self.store.callback_stats(task_id))
        if row['post_completion'] is not None:
            result['postCompletion'] = json.loads(row['post_completion'])
        return result

    async def request_cancel(self, task_id, reason):
        async with self.lock:
            current = self.query(task_id)
            if current["status"] in TERMINAL and self.active != task_id:
                return dict(code=0, message="already finished", taskId=task_id, status=current["status"])
            self.store.request_cancel(task_id, reason)
            if self.active == task_id:
                self.cancel.set()
            return dict(code=0, message="cancellation requested", taskId=task_id, status=current["status"])

    async def execute(self, request, cancel):
        task_id = request.taskId
        gear_progress = 0
        try:
            self.store.transition(task_id, "RUNNING", 0, "running")

            async def report(progress, message):
                nonlocal gear_progress
                if request.taskType == 'GEAR_OPERATION':
                    if type(progress) is not int or not gear_progress <= progress <= request.total:
                        raise ValueError('Invalid gear cycle progress')
                    gear_progress = progress
                    # Publish the last increment together with SUCCEEDED, only
                    # after the executor's return and endpoint check finish.
                    progress = min(progress, request.total - 1)
                self.store.transition(task_id, "RUNNING", progress, message)

            result = await self.executor.execute(request, cancel, report)
            if result.status not in TERMINAL:
                raise ValueError("controller returned a nonterminal result")
            error_code = (result.error_code or "2004") if result.status == "FAILED" else None
            progress = result.progress
            if request.taskType == 'GEAR_OPERATION' and result.status != 'SUCCEEDED':
                progress = min(progress, request.total - 1)
            self.store.transition(task_id, result.status, progress, result.message, error_code)
            if request.taskType == "ROBOT_INITIALIZE":
                self.initialized = result.status == "SUCCEEDED"
            elif result.status == "FAILED" or (request.taskType == 'GEAR_OPERATION' and result.status == 'CANCELLED'):
                self.initialized = False
        except Exception:
            logger.exception("Task %s failed", task_id)
            self.initialized = False
            progress = self.query(task_id)["progress"]
            self.store.transition(task_id, "FAILED", progress,
                                  "Controller failed or became unavailable; initialize again", "2004")
        finally:
            self.active = None

    async def deliver_callbacks(self):
        while True:
            row = self.store.pending_callback(time.time())
            if row is None:
                await asyncio.sleep(0.05)
                continue
            success, error = False, None
            try:
                response = await self.client.post(row["url"], content=row["payload"],
                                                  headers={"Content-Type": "application/json"})
                response.raise_for_status()
                body = response.json()
                success = isinstance(body, dict) and type(body.get("code")) is int and body["code"] == 0
                if not success:
                    error = "ELN did not acknowledge with integer code=0"
            except (httpx.HTTPError, ValueError) as exc:
                error = type(exc).__name__
            retry_delay = self.settings.callback_retry_seconds * 2 ** min(row["attempts"], 16)
            if row['retry_forever']:
                retry_delay = min(retry_delay, 60.0)
            self.store.callback_result(row, success, error, time.time() + retry_delay)
            if not success:
                logger.warning("Callback task=%s attempt=%s failed: %s", row["task_id"], row["attempts"] + 1, error)

    async def close(self):
        self.stopping = True
        if self.worker and not self.worker.done():
            self.cancel.set()
            await self.worker
        self.callback_worker.cancel()
        await asyncio.gather(self.callback_worker, return_exceptions=True)
        await self.client.aclose()
        await self.executor.close()
        self.store.close()
