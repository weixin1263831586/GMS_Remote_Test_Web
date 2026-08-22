from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.system.health import readiness, reset_health_cache


def _app_for(data_root: Path):
    dispatcher = SimpleNamespace(done=lambda: False)
    return SimpleNamespace(
        state=SimpleNamespace(
            services=SimpleNamespace(
                settings=SimpleNamespace(data_root=data_root),
            ),
            usb_event_queue=SimpleNamespace(qsize=lambda: 0),
            usb_dispatch_task=dispatcher,
        )
    )


class HealthReadinessTests(unittest.TestCase):
    def setUp(self):
        reset_health_cache()

    def tearDown(self):
        reset_health_cache()

    def _healthy_capability_patches(self):
        return (
            patch('features.system.health._check_adb', return_value={'ok': True}),
            patch('features.system.health._check_ssh', return_value={'ok': True}),
            patch(
                'features.system.health._check_automation_worker',
                return_value={'ok': True, 'enabled': False},
            ),
            patch(
                'features.system.health._check_local_worker',
                return_value={'ok': True, 'required': False},
            ),
        )

    def test_readiness_uses_injected_runtime_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'injected-runtime'
            app = _app_for(root)
            patches = self._healthy_capability_patches()
            with patches[0], patches[1], patches[2], patches[3]:
                result = readiness(app, force=True)

            self.assertTrue(result['ready'])
            self.assertEqual(result['data_root'], str(root.resolve()))
            self.assertTrue((root / 'health/health.sqlite3').is_file())

    def test_cache_does_not_cross_application_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            first_root = Path(directory) / 'first'
            second_root = Path(directory) / 'second'
            patches = self._healthy_capability_patches()
            with patches[0], patches[1], patches[2], patches[3]:
                first = readiness(_app_for(first_root))
                second = readiness(_app_for(second_root))

            self.assertEqual(first['data_root'], str(first_root.resolve()))
            self.assertEqual(second['data_root'], str(second_root.resolve()))
            self.assertTrue((first_root / 'health/health.sqlite3').is_file())
            self.assertTrue((second_root / 'health/health.sqlite3').is_file())

    def test_optional_capability_failures_report_degraded_but_stay_ready(self):
        app = _app_for(Path('/tmp/gms-health-test'))
        with (
            patch('features.system.health._check_storage', return_value={'ok': True}),
            patch('features.system.health._check_database_write', return_value={'ok': True}),
            patch('features.system.health._check_adb', return_value={'ok': False}),
            patch('features.system.health._check_ssh', return_value={'ok': False}),
            patch(
                'features.system.health._check_automation_worker',
                return_value={'ok': True, 'enabled': False},
            ),
            patch(
                'features.system.health._check_local_worker',
                return_value={'ok': True, 'required': False},
            ),
            patch(
                'features.system.health._check_runtime_queues',
                return_value={'ok': False, 'initialized': False},
            ),
        ):
            result = readiness(app, force=True)

        self.assertTrue(result['ready'])
        self.assertEqual(result['failed_required_checks'], [])
        self.assertEqual(
            result['degraded_checks'],
            ['adb', 'runtime_queues', 'ssh'],
        )

    def test_required_persistence_failure_returns_not_ready(self):
        app = _app_for(Path('/tmp/gms-health-test'))
        with (
            patch('features.system.health._check_storage', return_value={'ok': True}),
            patch('features.system.health._check_database_write', return_value={'ok': False}),
            patch('features.system.health._check_adb', return_value={'ok': True}),
            patch('features.system.health._check_ssh', return_value={'ok': True}),
            patch(
                'features.system.health._check_automation_worker',
                return_value={'ok': True, 'enabled': False},
            ),
            patch(
                'features.system.health._check_local_worker',
                return_value={'ok': True, 'required': False},
            ),
            patch(
                'features.system.health._check_runtime_queues',
                return_value={'ok': True},
            ),
        ):
            result = readiness(app, force=True)

        self.assertFalse(result['ready'])
        self.assertEqual(result['failed_required_checks'], ['database'])


if __name__ == '__main__':
    unittest.main()
