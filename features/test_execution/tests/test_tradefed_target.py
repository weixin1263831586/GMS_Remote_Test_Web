from unittest.mock import patch

import pytest

from features.test_execution.suite_helpers import resolve_tradefed_target


SUITE = {'tools_path': '/suites/android-cts/tools', 'full_path': '/suites/android-cts/tools/cts-tradefed',
         'version': 'android-cts', 'binary': 'cts-tradefed'}


@pytest.mark.parametrize('path,binary', [('/tmp', '/bin/sh'), ('/suites/android-cts/tools', '/bin/sh'),
                                      ('/suites/android-cts/tools/../../tmp', 'cts-tradefed')])
def test_ws_tradefed_target_rejects_paths_or_executables_outside_inventory(path, binary):
    with patch('features.test_execution.suite_helpers.get_available_test_suites', return_value=[SUITE]), \
            pytest.raises(ValueError):
        resolve_tradefed_target({}, path, binary)


@pytest.mark.parametrize('path', ['android-cts', '/suites/android-cts/tools'])
def test_tradefed_target_resolves_to_inventory_executable(path):
    with patch('features.test_execution.suite_helpers.get_available_test_suites', return_value=[SUITE]):
        assert resolve_tradefed_target({}, path, 'cts-tradefed') == (SUITE['tools_path'], SUITE['full_path'])
