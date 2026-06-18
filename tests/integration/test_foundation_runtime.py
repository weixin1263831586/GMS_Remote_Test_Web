import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from foundation.cache import TTLCache
from foundation.config import ConfigManager, RuntimeSettings
from foundation.tasks import SingleFlightTask


class FoundationConfigTests(unittest.TestCase):
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
