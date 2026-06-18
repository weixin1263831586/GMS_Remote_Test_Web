import unittest

from features.test_execution.service import (
    DuplicateExecutionError,
    TestExecutionService,
)


class TestExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.released = []
        self.service = TestExecutionService(
            release_devices=lambda client_id, devices: self.released.append(
                (client_id, list(devices))
            )
        )

    async def test_start_rejects_duplicate_active_run(self):
        await self.service.start("client", ["D1"])

        with self.assertRaises(DuplicateExecutionError):
            await self.service.start("client", ["D2"])

    async def test_stop_is_idempotent(self):
        first = await self.service.stop("client")
        second = await self.service.stop("client")

        self.assertFalse(first["was_running"])
        self.assertFalse(second["was_running"])

    async def test_clean_resets_logs_and_status(self):
        await self.service.start("client", ["D1"])
        self.service.append_log("client", "line")

        state = await self.service.clean("client")

        self.assertFalse(state["running"])
        self.assertEqual(state["devices"], [])
        self.assertEqual(state["logs"], [])

    async def test_failed_start_releases_devices(self):
        async def fail():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await self.service.start("client", ["D1"], on_started=fail)

        self.assertEqual(self.released, [("client", ["D1"])])
        self.assertFalse(self.service.status("client")["running"])

    async def test_stream_ends_after_stop(self):
        await self.service.start("client", ["D1"])
        stream = self.service.stream("client", poll_interval=0)
        self.service.append_log("client", "line")

        first = await anext(stream)
        await self.service.stop("client")
        remaining = [item async for item in stream]

        self.assertEqual(first, "line")
        self.assertEqual(remaining, [])

    async def test_task_status_survives_polling(self):
        task_id = self.service.create_task("download")
        self.service.update_task(task_id, status="running", progress=50)

        first = self.service.get_task(task_id)
        second = self.service.get_task(task_id)

        self.assertEqual(first, second)
        self.assertEqual(second["progress"], 50)
