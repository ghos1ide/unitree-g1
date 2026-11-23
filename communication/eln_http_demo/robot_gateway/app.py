import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field, ValidationError

from .adapters import LocalExecutor, MockControlAdapter
from .config import Settings
from .manager import BusinessError, TaskManager
from .models import CancelRequest, ControlRequest, InitializeRequest, SimulationConfig, TaskRequest
from .store import Store


def create_app(settings=None, executor=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        selected = executor
        if selected is None:
            if settings.backend == "mock":
                selected = LocalExecutor(MockControlAdapter(settings.mock_step_seconds))
            elif settings.backend == "ros2":
                from .ros2_executor import Ros2Executor
                selected = Ros2Executor(settings)
            else:
                raise ValueError("ROBOT_BACKEND must be mock or ros2")
        manager = TaskManager(settings, Store(settings.database, settings.reliable_callbacks), selected)
        app.state.manager = manager
        await manager.start()
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(title="Robot ELN Task Gateway", version="1.5.0", lifespan=lifespan)

    class CallbackTarget(InitializeRequest):
        taskType: str = Field(min_length=1, max_length=128)

    async def rejection(request, code, message, body=None):
        if not settings.reliable_callbacks:
            return {}
        path = request.url.path
        submission = path in ('/api/v1/tasks', '/api/v1/robot/initialize') or (
            path.startswith('/api/v1/actions/') and path.endswith('/tasks'))
        if request.method != 'POST' or not submission:
            return {}
        result = dict(status='FAILED', callbackScheduled=False)
        if code == 1002:
            return result
        try:
            body = await request.json() if body is None else body
            if not isinstance(body, dict):
                return result
            task_type = ('ROBOT_INITIALIZE' if path.endswith('/initialize') else
                         'RECORDED_ACTION' if path.startswith('/api/v1/actions/') else body.get('taskType'))
            target = CallbackTarget(taskId=body.get('taskId'), taskType=task_type,
                                    callbackUrl=body.get('callbackUrl'))
            if target.callbackUrl.username or target.callbackUrl.password or target.callbackUrl.fragment:
                return result
        except (ValueError, TypeError):
            return result
        manager = request.app.state.manager
        async with manager.lock:
            event, created = manager.store.reject(target.model_dump(mode='json'), message, code)
        return dict(taskId=target.taskId, status=manager.store.final_event(event)['status'],
                    callbackScheduled=created, duplicate=not created)

    if settings.reliable_callbacks:
        class ReliableResultRoute(APIRoute):
            def get_route_handler(self):
                handler = super().get_route_handler()

                async def handle(request):
                    try:
                        return await handler(request)
                    except (BusinessError, RequestValidationError):
                        raise
                    except Exception:
                        logging.getLogger(__name__).exception('Request failed before a result was returned')
                        extra = await rejection(request, 9000, 'Internal error; request was not completed')
                        return JSONResponse(status_code=500, content=dict(
                            code=9000, message='Internal error; query taskId before retrying', **extra))
                return handle

        app.router.route_class = ReliableResultRoute

    @app.exception_handler(BusinessError)
    async def business_error(request: Request, exc: BusinessError):
        extra = await rejection(request, exc.code, exc.message)
        return JSONResponse(status_code=exc.http_status, content=dict(code=exc.code, message=exc.message, **extra))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [dict(field=".".join(map(str, e["loc"])), message=e["msg"]) for e in exc.errors()]
        message = 'invalid parameters: ' + '; '.join(d['field'] + ': ' + d['message'] for d in details)
        extra = await rejection(request, 1001, message, exc.body)
        return JSONResponse(status_code=400, content=dict(code=1001, message="invalid parameters", details=details, **extra))

    async def authorize(x_api_key: Optional[str] = Header(default=None)):
        if settings.api_key and not secrets.compare_digest((x_api_key or "").encode(), settings.api_key.encode()):
            raise BusinessError(1002, "unauthorized", 401)

    auth = [Depends(authorize)]

    @app.get("/health")
    async def health():
        manager = app.state.manager
        return dict(status="ok", backend=settings.backend, initialized=manager.initialized,
                    activeTaskId=manager.active, simulation=settings.backend == "mock" or settings.simulation,
                    protocolVersion="1.5")

    @app.post("/api/v1/tasks", dependencies=auth)
    async def submit(request: ControlRequest):
        return await app.state.manager.submit(request)

    @app.post("/api/v1/robot/initialize", dependencies=auth)
    async def initialize(request: InitializeRequest):
        try:
            task = TaskRequest(taskId=request.taskId, taskType="ROBOT_INITIALIZE", callbackUrl=request.callbackUrl, parameters={})
        except ValidationError:
            raise BusinessError(1001, "invalid callbackUrl", 400)
        return await app.state.manager.submit(task)

    @app.post("/api/v1/tasks/{task_id}/query", dependencies=auth)
    async def query(task_id: str):
        return app.state.manager.query(task_id)

    @app.post("/api/v1/tasks/{task_id}/cancel", dependencies=auth)
    async def cancel(task_id: str, request: CancelRequest):
        return await app.state.manager.request_cancel(task_id, request.reason)

    @app.post("/api/v1/simulation/config", dependencies=auth, tags=["Simulation Extension"])
    async def simulation(config: SimulationConfig):
        if settings.backend != "mock" and not settings.simulation:
            raise BusinessError(1001, "simulation is disabled", 400)
        manager = app.state.manager
        async with manager.lock:
            if manager.active:
                raise BusinessError(2001, "robot busy")
            manager.executor.configure_simulation(config)
        return dict(code=0, message="configured", **config.model_dump())

    return app


app = create_app()
