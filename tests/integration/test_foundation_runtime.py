import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from foundation.cache import TTLCache
from foundation.config import ConfigManager, RuntimeSettings
from foundation.runtime_settings import allowed_origins, runtime_environment
from foundation.tasks import SingleFlightTask


class FoundationConfigTests(unittest.TestCase):
    def test_environment_and_cors_helpers_use_canonical_variables(self):
        environ = {
            'GMS_ENV': ' Production ',
            'GMS_ALLOWED_ORIGINS': 'https://one.example, https://two.example ',
        }
        self.assertEqual(runtime_environment(environ), 'production')
        self.assertEqual(
            allowed_origins(environ),
            ['https://one.example', 'https://two.example'],
        )

    def test_concurrent_incremental_updates_preserve_unrelated_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'configs').mkdir()
            (root / 'foundation').mkdir()
            manager = ConfigManager(project_root=root)
            barrier = threading.Barrier(2)

            def update(key, value):
                barrier.wait()
                self.assertTrue(manager.update_runtime_config({key: value}))

            threads = [
                threading.Thread(target=update, args=('sidebar_order', ['test'])),
                threading.Thread(target=update, args=('redmine_stats', {'window_days': 30})),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(
                manager.get_runtime_config(),
                {
                    'sidebar_order': ['test'],
                    'redmine_stats': {'window_days': 30},
                },
            )

    def test_failed_runtime_write_preserves_previous_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configs = root / 'configs'
            configs.mkdir()
            (root / 'foundation').mkdir()
            runtime_path = configs / 'config_runtime.json'
            runtime_path.write_text('{"existing": true}', encoding='utf-8')
            manager = ConfigManager(project_root=root)

            with patch(
                'foundation.config.json.dump',
                side_effect=OSError('disk full'),
            ):
                saved = manager.save_runtime_config({'replacement': True})

            self.assertFalse(saved)
            self.assertEqual(
                json.loads(runtime_path.read_text(encoding='utf-8')),
                {'existing': True},
            )
            self.assertEqual(list(configs.glob('*.tmp')), [])

    def test_runtime_write_does_not_mutate_callers_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configs = root / 'configs'
            configs.mkdir()
            (root / 'foundation').mkdir()
            runtime_path = configs / 'config_runtime.json'
            runtime_path.write_text(
                '{"redmine_auth": {"encrypted_password": "secret"}}',
                encoding='utf-8',
            )
            manager = ConfigManager(project_root=root)
            payload = {'sidebar_order': ['test']}

            self.assertTrue(manager.save_runtime_config(payload))

            self.assertEqual(payload, {'sidebar_order': ['test']})
            stored = json.loads(
                runtime_path.read_text(encoding='utf-8')
            )
            self.assertIn('redmine_auth', stored)

    def test_runtime_settings_support_injected_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = RuntimeSettings.from_environment(
                project_root=Path(tmp),
                environ={'GMS_DATA_ROOT': str(Path(tmp) / 'runtime')},
            )
            self.assertEqual(settings.data_root, Path(tmp) / 'runtime')

    def test_config_manager_reads_existing_config_directory(self):
        root = Path(__file__).resolve().parents[2]
        manager = ConfigManager(project_root=root)
        self.assertIn('redmine', manager.load_config(force_reload=True))

    def test_example_config_fallback_keeps_cache_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configs = root / 'configs'
            configs.mkdir()
            (root / 'foundation').mkdir()
            (configs / 'config.example.json').write_text(
                '{"ubuntu_user": "operator"}', encoding='utf-8'
            )
            manager = ConfigManager(project_root=root)

            self.assertEqual(
                manager.load_config(force_reload=True)['ubuntu_user'],
                'operator',
            )
            self.assertTrue(manager._is_cache_valid(time.time()))


class TTLCacheTests(unittest.TestCase):
    def test_expired_value_is_removed(self):
        cache = TTLCache(ttl_seconds=0.01)
        cache.set('key', 'value')
        time.sleep(0.02)
        self.assertIsNone(cache.get('key'))

    def test_clear_invalidates_all_values(self):
        cache = TTLCache(ttl_seconds=10)
        cache.set('first', 1)
        cache.set('second', 2)
        cache.clear()
        self.assertIsNone(cache.get('first'))
        self.assertIsNone(cache.get('second'))


class SingleFlightTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_start_returns_existing_task(self):
        gate = asyncio.Event()

        async def operation():
            await gate.wait()
            return 'done'

        runner = SingleFlightTask()
        first = await runner.start(operation)
        second = await runner.start(operation)
        self.assertIs(first, second)
        gate.set()
        self.assertEqual(await first, 'done')

    async def test_cancel_clears_running_state(self):
        gate = asyncio.Event()

        async def operation():
            await gate.wait()

        runner = SingleFlightTask()
        await runner.start(operation)
        await runner.cancel()
        self.assertFalse(runner.running)
