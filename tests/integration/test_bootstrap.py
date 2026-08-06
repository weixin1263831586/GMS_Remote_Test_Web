import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from bootstrap.dependencies import build_services
from foundation.runtime_settings import RuntimeSettings


class BootstrapTests(unittest.TestCase):
    def test_create_app_preserves_metadata(self):
        app = create_app()
        self.assertEqual(app.title, 'GMS Auto Test - FastAPI Server (Port 5001)')
        self.assertEqual(app.version, '4.0.0')

    def test_create_app_registers_health_route(self):
        app = create_app()
        paths = {route.path for route in app.routes}
        self.assertIn('/api/system/health', paths)

    def test_requests_return_stable_request_and_trace_ids(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {'GMS_DATA_ROOT': directory}),
        ):
            runtime_settings = RuntimeSettings.from_environment()
            services = build_services(runtime_settings=runtime_settings)
            with TestClient(create_app(services)) as client:
                response = client.get(
                    '/api/system/health',
                    headers={
                        'X-Request-ID': 'req-ui-1',
                        'X-Trace-ID': 'ats-run-1',
                    },
                )

        self.assertEqual(response.headers['X-Request-ID'], 'req-ui-1')
        self.assertEqual(response.headers['X-Trace-ID'], 'ats-run-1')

    def test_development_network_host_can_load_shell_and_favicon(self):
        with patch.dict(os.environ):
            os.environ.pop('GMS_ENV', None)
            os.environ.pop('TRUSTED_HOSTS', None)
            client = TestClient(create_app())
            page = client.get('/', headers={'Host': '172.16.14.233'})
            favicon = client.get(
                '/favicon.ico',
                headers={'Host': '172.16.14.233'},
            )
            client.close()

        self.assertEqual(page.status_code, 200)
        self.assertEqual(favicon.status_code, 200)
        self.assertEqual(favicon.headers['content-type'], 'image/svg+xml')

    def test_development_auth_defaults_to_anonymous_client_mode(self):
        with patch.dict(os.environ):
            os.environ.pop('GMS_ENV', None)
            os.environ.pop('GMS_AUTH_REQUIRED', None)
            client = TestClient(create_app())
            status = client.get('/api/auth/status')
            current = client.get(
                '/api/users/current',
                headers={'Host': '172.16.14.233'},
            )
            workspace = client.get(
                '/api/users/workspace-context',
                headers={'Host': '172.16.14.233'},
            )
            client.close()

        self.assertFalse(status.json()['auth_required'])
        self.assertEqual(current.status_code, 200)
        self.assertEqual(workspace.status_code, 200)

    def test_development_admin_scoped_reads_keep_anonymous_compatibility(self):
        with patch.dict(os.environ):
            os.environ.pop('GMS_ENV', None)
            os.environ.pop('GMS_AUTH_REQUIRED', None)
            client = TestClient(create_app())
            responses = {
                path: client.get(path)
                for path in (
                    '/api/users/list',
                    '/api/vpn/status',
                    '/api/vpn/connections',
                    '/api/test/suites/archives',
                )
            }
            client.close()

        for path, response in responses.items():
            with self.subTest(path=path):
                self.assertNotIn(response.status_code, (401, 403))

    def test_create_app_keeps_automation_cluster_preflight_enabled(self):
        from features.automation import api as automation
        from features.cluster import get_cluster_service

        create_app()
        self.assertIs(
            automation.automation_service._cluster_provider,
            get_cluster_service,
        )

    def test_create_app_preserves_frozen_routes(self):
        from tests.contract.snapshot_tools import normalized_routes, read_json

        app = create_app()
        self.assertEqual(normalized_routes(app), read_json('routes.json'))
