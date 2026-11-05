"""Configuration contracts create their own deployment roots, independent of the runner."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_configuration_contract_roots(request, monkeypatch):
    if Path(str(request.node.fspath)).stem in {
        'test_config_examples', 'test_config_migration', 'test_config_paths',
    }:
        # The application's imported RuntimeSettings remains isolated by the
        # runner. These tests deliberately exercise project_root/data defaults.
        monkeypatch.delenv('GMS_DATA_ROOT', raising=False)
