"""LAN entry point for the real API. Forwards once; never retries motion requests."""
import argparse
import asyncio
import logging
import time
from contextlib import asynccontextmanager


def create_app(upstream='http://127.0.0.1:8795', transport=None, upstream_api_key=None, history=None):
    import httpx
    from fastapi import FastAPI, Request
    from starlette.responses import JSONResponse, Response

    client = httpx.AsyncClient(base_url=upstream, trust_env=False, timeout=60., transport=transport)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)
    excluded = {'host', 'connection', 'transfer-encoding', 'content-length', 'content-encoding',
                'keep-alive', 'te', 'trailer', 'upgrade', 'proxy-authorization', 'proxy-authenticate'}

    async def forward(request):
        started = time.monotonic()
        request_id = None
        audit_failed = False

        def record(method, *args, **kwargs):
            nonlocal audit_failed
            if history is None:
                return None
            try:
                return getattr(history, method)(*args, **kwargs)
            except Exception:
                audit_failed = True
                logging.getLogger(__name__).exception('HTTP request history write failed')
                return None

        peer = '%s:%s' % (request.client.host, request.client.port) if request.client else 'unknown'
        request_id = record('start', request.method, request.url.path, peer)

        def step(stage, message, **fields):
            if request_id:
                record('update', request_id, stage, message, **fields)

        def finish(response, stage=None, message=None):
            if history is not None and request_id:
                payload = history.summarize(response.body)
                if request.url.path == '/health' and isinstance(payload, dict) and response.status_code == 200:
                    payload = {key: payload[key] for key in (
                        'backend', 'initialized', 'activeTaskId', 'requiresRecovery', 'controllerRunning') if key in payload}
                detected_stage, detected_message = history.diagnose(response.status_code, payload)
                fields = dict(httpStatus=response.status_code, responseBody=payload,
                              durationMs=round((time.monotonic() - started) * 1000, 1), finished=True)
                if isinstance(payload, dict):
                    fields['businessCode'] = payload.get('code')
                    fields['businessMessage'] = payload.get('message', payload.get('detail'))
                    submission = request.method == 'POST' and (request.url.path in (
                        '/api/v1/tasks', '/api/v1/robot/initialize') or
                        request.url.path.startswith('/api/v1/actions/') and request.url.path.endswith('/tasks'))
                    fields['acceptance'] = ('accepted' if submission and payload.get('status') == 'ACCEPTED' and payload.get('code') == 0
                                            else 'rejected' if submission and response.status_code in (400, 401, 403, 404, 409, 422)
                                            else 'unknown' if submission else 'not_applicable')
                step(stage or detected_stage, message or detected_message, **fields)
                response.headers['X-Request-ID'] = request_id
            if audit_failed:
                response.headers['X-Request-History'] = 'unavailable'
            return response

        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    return finish(JSONResponse({'error': 'Request too large'}, status_code=413),
                                  'body_error', 'Request exceeds 1 MiB; not forwarded')
        except asyncio.CancelledError:
            step('interrupted', 'Request cancelled while reading body', finished=True)
            raise
        except Exception:
            step('body_error', 'Client disconnected or request body could not be read', finished=True)
            raise
        if history is not None:
            payload = history.summarize(body)
            metadata = {'requestBody': payload, 'bodyBytes': len(body)}
            if isinstance(payload, dict):
                metadata.update({k: payload[k] for k in ('taskId', 'taskType') if isinstance(payload.get(k), str)})
            step('body_received', 'Request body received', **metadata)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded}
        if upstream_api_key is not None:
            headers['x-api-key'] = upstream_api_key
        if request_id:
            headers['x-request-id'] = request_id
        path = request.url.path
        if request.url.query:
            path += '?' + request.url.query
        try:
            step('forwarding', 'Forwarding once to the execution API')
            response = await client.request(request.method, path, content=bytes(body), headers=headers)
        except httpx.RequestError as error:
            return finish(JSONResponse({'code': 2004, 'message': 'Real executor unavailable or outcome unknown; '
                                        'query the same taskId before resubmitting. No retry was sent.'}, status_code=502),
                          'transport_error', type(error).__name__ + ': execution API connection failed or response timed out')
        except asyncio.CancelledError:
            step('interrupted', 'Forwarding interrupted; execution outcome unknown', finished=True, outcomeUnknown=True)
            raise
        except Exception:
            step('interrupted', 'Unexpected relay error; execution outcome unknown', finished=True, outcomeUnknown=True)
            raise
        step('upstream_response', 'Execution API returned HTTP %s' % response.status_code)
        return finish(Response(response.content, status_code=response.status_code,
                               headers={k: v for k, v in response.headers.items() if k.lower() not in excluded}))

    # Annotation must resolve locally on Python 3.8 without global FastAPI imports.
    forward.__annotations__['request'] = Request
    app.add_api_route('/{path:path}', forward, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD', 'TRACE', 'CONNECT'])
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--upstream', default='http://127.0.0.1:8795')
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.upstream), host=args.host, port=args.port, workers=1)
