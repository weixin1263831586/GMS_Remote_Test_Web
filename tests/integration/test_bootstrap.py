import unittest

from bootstrap.application import create_app


class BootstrapTests(unittest.TestCase):
    def test_create_app_preserves_metadata(self):
        app = create_app()
        self.assertEqual(app.title, 'GMS Auto Test - FastAPI Server (Port 5001)')
        self.assertEqual(app.version, '4.0.0')

    def test_create_app_registers_health_route(self):
        app = create_app()
        paths = {route.path for route in app.routes}
        self.assertIn('/api/system/health', paths)

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
