import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.test_execution import execution_api
from features.test_execution.models import TestStartRequest as StartRequest


class TestStartConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_start_for_same_client_is_rejected(self):
        state = {}
        acquisition_started = asyncio.Event()
        allow_acquisition = asyncio.Event()

        async def acquire(**_kwargs):
            acquisition_started.set()
            await allow_acquisition.wait()
            return ['SERIAL-1'], []

        async def background(*_args):
            return None

        global_state = SimpleNamespace(
            user_states_lock=threading.RLock(),
            background_tasks=set(),
        )
        config_manager = SimpleNamespace(load_config=lambda: {})
        request = SimpleNamespace()
        req = StartRequest(test_type='cts', devices=['SERIAL-1'])

        with (
            patch.object(execution_api.runtime, 'global_state', global_state),
            patch.object(execution_api.runtime, 'config_manager', config_manager),
            patch.object(execution_api.runtime, 'generate_help_or_continue', return_value=None),
            patch.object(execution_api.runtime, 'get_client_id_from_request', return_value='alice'),
            patch.object(execution_api.runtime, 'acquire_test_devices', side_effect=acquire),
            patch.object(execution_api, 'get_client_username_from_request', return_value='alice'),
            patch.object(execution_api, 'get_client_display_id_from_request', return_value='alice'),
            patch.object(execution_api, 'get_or_create_user_state', return_value=state),
            patch.object(execution_api, 'update_user_state_field', side_effect=lambda _id, data: state.update(data)),
            patch.object(execution_api, '_run_test_background', side_effect=background),
        ):
            first_task = asyncio.create_task(
                execution_api.start_test(request, req=req)
            )
            await acquisition_started.wait()
            second = await execution_api.start_test(request, req=req)
            allow_acquisition.set()
            first = await first_task
            await asyncio.sleep(0)

        self.assertEqual(second.status_code, 400)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(state['starting'])
        self.assertTrue(state['running'])


if __name__ == '__main__':
    unittest.main()
