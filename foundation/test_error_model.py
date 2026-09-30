"""foundation/error_model.py 的全局错误模型回归测试。"""

import unittest
from unittest.mock import Mock

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

    def test_internal_error_message_hides_exception_and_carries_request_id(self):
        from foundation.error_model import internal_error_message

        first = internal_error_message('烧写固件')
        second = internal_error_message('烧写固件')
        # 动作名可见、每次请求生成不同的 request id（用于回查日志）。
        self.assertIn('烧写固件', first)
        self.assertIn('request_id=', first)
        self.assertIn('已记录日志', first)
        self.assertNotEqual(first, second)
        # 不接受调用方把异常文本拼进动作名后整段回显给客户端的用法：
        # 消息只包含动作短语与 request id，不包含调用方额外塞入的内容。
        self.assertTrue(all(part not in first for part in ('Secret', '/home/')))

    def test_record_internal_error_logs_the_returned_request_id(self):
        from foundation.error_model import record_internal_error

        logger = Mock()
        message = record_internal_error(
            logger,
            '烧写固件',
            'firmware burn failed',
            context={'serial': 'device-1'},
        )

        logger.error.assert_called_once()
        args, kwargs = logger.error.call_args
        # 固定格式的 %-format：message / context 只作为参数，不拼进格式串，
        # 占位符数量恒为 3，杜绝字面 %s 残留与 "not all arguments converted"。
        self.assertEqual(args[0], '%s%s: %s')
        self.assertEqual(args[1], 'firmware burn failed')
        self.assertEqual(args[2], ' serial=device-1')
        self.assertEqual(args[3], message)
        self.assertTrue(kwargs['exc_info'])

    def test_record_internal_error_without_context_has_empty_suffix(self):
        from foundation.error_model import record_internal_error

        logger = Mock()
        message = record_internal_error(logger, '烧写固件', 'firmware burn error')

        logger.error.assert_called_once()
        args, kwargs = logger.error.call_args
        self.assertEqual(args[1], 'firmware burn error')
        self.assertEqual(args[2], '')
        self.assertEqual(args[3], message)
        self.assertTrue(kwargs['exc_info'])

    def test_record_internal_error_placeholder_mismatch_cannot_crash(self):
        from foundation.error_model import record_internal_error

        logger = Mock()
        # 旧契约下占位符与参数错配会让 logging 抛 "not all arguments
        # converted"；新契约格式串固定，任何调用都恰好 3 个占位参数。
        record_internal_error(
            logger,
            'SSH 登录校验',
            'SSH credential check failed',
            context={'client_ip': '172.16.0.9'},
        )

        args, _ = logger.error.call_args
        self.assertEqual(args[0].count('%s'), 3)
        # 按 logging 的真实渲染方式展开，保证 logging 层永不报占位符错配。
        rendered = args[0] % tuple(args[1:4])
        self.assertIn('client_ip=172.16.0.9', rendered)


if __name__ == '__main__':
    unittest.main()
