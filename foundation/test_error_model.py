"""foundation/error_model.py 的全局错误模型回归测试。"""

import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from foundation.error_model import API_ERROR_CODES, ApiError, register_api_error_handler
from foundation.errors import handle_api_errors


class ApiErrorCodeTableTests(unittest.TestCase):
    def test_status_mapping_matches_review_contract(self):
        # Infrastructure failures use their semantic status instead of 500.
        self.assertEqual(API_ERROR_CODES['MALFORMED_REQUEST'], 400)
        self.assertEqual(API_ERROR_CODES['UNAUTHENTICATED'], 401)
        self.assertEqual(API_ERROR_CODES['FORBIDDEN'], 403)
        self.assertEqual(API_ERROR_CODES['NOT_FOUND'], 404)
        self.assertEqual(API_ERROR_CODES['STATE_CONFLICT'], 409)
        self.assertEqual(API_ERROR_CODES['INVALID_SEMANTICS'], 422)
        self.assertEqual(API_ERROR_CODES['INTERNAL_ERROR'], 500)
        self.assertEqual(API_ERROR_CODES['UPSTREAM_FAILURE'], 502)
        self.assertEqual(API_ERROR_CODES['DEPENDENCY_UNAVAILABLE'], 503)
        self.assertEqual(API_ERROR_CODES['DEPENDENCY_TIMEOUT'], 504)

    def test_unknown_code_rejected(self):
        with self.assertRaises(ValueError):
            ApiError(code='NOT_A_REAL_CODE', message='x')

    def test_envelope_contains_code_and_next_actions(self):
        exc = ApiError.upstream_failure(
            'SSH connection failed',
            service='ssh',
            next_actions=[{'action': 'retry later'}],
        )
        payload = exc.envelope()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['code'], 'UPSTREAM_FAILURE')
        self.assertEqual(payload['error'], 'SSH connection failed')
        self.assertEqual(payload['details'], {'service': 'ssh'})
        self.assertEqual(payload['next_actions'], [{'action': 'retry later'}])
        self.assertEqual(exc.to_response().status_code, 502)


class HandlerIntegrationTests(unittest.TestCase):
    def _app(self) -> FastAPI:
        app = FastAPI()
        register_api_error_handler(app)

        @app.get('/upstream')
        async def upstream():
            raise ApiError.upstream_failure('worker unavailable', service='worker')

        @app.get('/legacy')
        @handle_api_errors
        async def legacy():
            raise ApiError.conflict('job already running')

        @app.get('/boom')
        @handle_api_errors
        async def boom():
            raise RuntimeError('unexpected')

        @app.get('/http-exc')
        @handle_api_errors
        async def http_exc():
            raise HTTPException(status_code=404, detail='absent')

        return app

    def test_global_handler_maps_code(self):
        client = TestClient(self._app(), raise_server_exceptions=False)
        resp = client.get('/upstream')
        self.assertEqual(resp.status_code, 502)
        body = resp.json()
        self.assertEqual(body['code'], 'UPSTREAM_FAILURE')
        self.assertFalse(body['success'])

    def test_decorated_route_maps_code(self):
        client = TestClient(self._app(), raise_server_exceptions=False)
        resp = client.get('/legacy')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['code'], 'STATE_CONFLICT')

    def test_unexpected_exception_still_500(self):
        client = TestClient(self._app(), raise_server_exceptions=False)
        resp = client.get('/boom')
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn('code', resp.json())

    def test_http_exception_passthrough(self):
        client = TestClient(self._app(), raise_server_exceptions=False)
        resp = client.get('/http-exc')
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main()
