import unittest

try:
    import httpx
    import fastapi
except ImportError:
    httpx = None
from http_relay import create_app


@unittest.skipIf(httpx is None, 'Run relay tests in the production API virtualenv')
class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_eln_entry_uses_internal_key(self):
        requests = []
        def backend(request):
            requests.append(request)
            if request.headers.get('X-API-Key') != 'internal-test-key':
                return httpx.Response(401, json={'code': 1002})
            return httpx.Response(409, json={'code': 2003, 'message': 'recovery required'})
        app = create_app(transport=httpx.MockTransport(backend), upstream_api_key='internal-test-key')
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://relay') as client:
                for headers in ({}, {'X-API-Key': 'obsolete-client-key'}):
                    result = await client.post('/api/v1/tasks', headers=headers,
                                               json={'taskId': 'same-id'})
                    self.assertEqual(result.status_code, 409)
                    self.assertEqual(result.json()['code'], 2003)
                    self.assertNotIn('internal-test-key', result.text)
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.headers.get_list('X-API-Key'), ['internal-test-key'])
            self.assertEqual(request.url.path, '/api/v1/tasks')
            self.assertEqual(request.content, requests[0].content)

    async def test_preserves_auth_body_path_and_business_status(self):
        requests = []
        def backend(request):
            requests.append(request)
            return httpx.Response(409, json={'code': 2003, 'message': 'recovery required'})
        app = create_app(transport=httpx.MockTransport(backend))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://relay') as client:
                result = await client.post('/api/v1/robot/initialize', headers={'X-API-Key': 'test-key'},
                                           json={'taskId': 'same-id', 'callbackUrl': 'http://pc/result'})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers['X-API-Key'], 'test-key')
        self.assertIn(b'same-id', requests[0].content)
        self.assertEqual(requests[0].url.path, '/api/v1/robot/initialize')

    async def test_unknown_outcome_never_retries(self):
        requests = []
        def backend(request):
            requests.append(request)
            raise httpx.ReadTimeout('outcome unknown', request=request)
        app = create_app(transport=httpx.MockTransport(backend))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://relay') as client:
                result = await client.post('/api/v1/actions/dring_left/tasks', json={'taskId': 'unique'})
        self.assertEqual(result.status_code, 502)
        self.assertEqual(len(requests), 1)
