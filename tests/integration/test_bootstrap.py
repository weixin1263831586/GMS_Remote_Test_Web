import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bootstrap.application import create_app
from bootstrap.dependencies import build_services
from foundation.runtime_settings import RuntimeSettings


class BootstrapTests(unittest.TestCase):
    def test_runtime_middleware_uses_canonical_environment_and_origins(self):
        with (
            patch.dict(
                os.environ,
                {
                    'GMS_ENV': 'production',
                    'GMS_ALLOWED_ORIGINS': 'https://gms.example',
                    'TRUSTED_HOSTS': 'gms.example',
                },
            ),
            patch(
                'bootstrap.application.validate_production_security_configuration'
            ),
        ):
            app = create_app()

        cors = next(item for item in app.user_middleware if item.cls is CORSMiddleware)
        trusted = next(
            item for item in app.user_middleware
            if item.cls is TrustedHostMiddleware
        )
        self.assertEqual(cors.kwargs['allow_origins'], ['https://gms.example'])
        self.assertEqual(trusted.kwargs['allowed_hosts'], ['gms.example'])

    def test_production_runtime_rejects_wildcard_trusted_hosts(self):
        with (
            patch.dict(
                os.environ,
                {'GMS_ENV': 'production', 'TRUSTED_HOSTS': '*'},
            ),
            patch(
                'bootstrap.application.validate_production_security_configuration'
            ),
            self.assertRaisesRegex(RuntimeError, 'TRUSTED_HOSTS'),
        ):
            create_app()

    def test_production_requires_agent_package_signing_key(self):
        """Production must fail closed without an Ed25519
        agent-package signing key — SHA-only serving is dev-only fallback."""
        import tempfile

        from cryptography.fernet import Fernet

        from bootstrap.production_security import (
            validate_production_security_configuration,
        )

        with tempfile.TemporaryDirectory() as data_root:
            production_env = {
                'GMS_ENV': 'production',
                'GMS_AUTH_REQUIRED': 'true',
                'GMS_SECURE_COOKIES': 'true',
                'GMS_BOOTSTRAP_TOKEN': 'b' * 48,
                'GMS_SECRET_KEY': Fernet.generate_key().decode('ascii'),
                'GMS_AUDIT_HMAC_KEY': 'a' * 64,
                'GMS_DATA_ROOT': data_root,
            }
            # The module-level audit singleton bound to the repo's real
            # data root at import time — swap in a fresh, empty one so the
            # audit-chain gate passes and the SIGNING gate is what fires.
            from foundation.security_audit import SecurityAuditLogger

            fresh_audit = SecurityAuditLogger(
                str(Path(data_root) / 'audit.jsonl')
            )
            with (
                patch.dict(os.environ, production_env),
                patch(
                    'bootstrap.production_security.security_audit_logger',
                    fresh_audit,
                ),
            ):
                os.environ.pop('GMS_SKILL_SIGNING_KEY_FILE', None)
                with self.assertRaisesRegex(
                    RuntimeError, 'GMS_SKILL_SIGNING_KEY_FILE'
                ):
                    validate_production_security_configuration()

    def test_production_signing_key_lets_validation_proceed(self):
        """With the Ed25519 key configured, the signing gate is transparent:
        validation continues and fails on the NEXT production requirement."""
        import tempfile

        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from bootstrap.production_security import (
            validate_production_security_configuration,
        )

        key_pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with tempfile.NamedTemporaryFile(
            'wb', suffix='.pem', delete=False
        ) as handle:
            handle.write(key_pem)
            key_path = handle.name
        self.addCleanup(os.unlink, key_path)

        with tempfile.TemporaryDirectory() as data_root:
            production_env = {
                'GMS_ENV': 'production',
                'GMS_AUTH_REQUIRED': 'true',
                'GMS_SECURE_COOKIES': 'true',
                'GMS_BOOTSTRAP_TOKEN': 'b' * 48,
                'GMS_SECRET_KEY': Fernet.generate_key().decode('ascii'),
                'GMS_AUDIT_HMAC_KEY': 'a' * 64,
                'GMS_SKILL_SIGNING_KEY_FILE': key_path,
                'GMS_DATA_ROOT': data_root,
            }
            from foundation.security_audit import SecurityAuditLogger

            fresh_audit = SecurityAuditLogger(
                str(Path(data_root) / 'audit.jsonl')
            )
            with (
                patch.dict(os.environ, production_env),
                patch(
                    'bootstrap.production_security.security_audit_logger',
                    fresh_audit,
                ),
                self.assertRaisesRegex(RuntimeError, 'GMS_METRICS_TOKEN'),
            ):
                # Signing gate passed → the next unmet requirement fires.
                validate_production_security_configuration()

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
