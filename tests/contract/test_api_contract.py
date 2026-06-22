import unittest

from app import app
from tests.contract.snapshot_tools import (
    normalized_openapi,
    normalized_routes,
    read_json,
)


class ApiContractTests(unittest.TestCase):
    def test_routes_match_frozen_contract(self):
        self.assertEqual(normalized_routes(app), read_json('routes.json'))

    def test_openapi_matches_frozen_contract(self):
        self.assertEqual(normalized_openapi(app), read_json('openapi.json'))
