"""Shared app/auth-service fixture for auth API test cases.

Plain mixin (NOT a TestCase subclass): sibling test modules import this
instead of subclassing test_auth_api's base suite, so pytest collects each
module's own tests exactly once.
"""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service
from features.auth.api import _client_ssh_probe_cache


class AuthApiMixin:
    def setUp(self):
        _client_ssh_probe_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = auth_service.db_path
        self.original_initialized = auth_service._initialized
        auth_service.db_path = Path(self.tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        _client_ssh_probe_cache.clear()
        auth_service.db_path = self.original_db_path
        auth_service._initialized = self.original_initialized
        self.tmp.cleanup()
