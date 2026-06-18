import tempfile
import unittest
from pathlib import Path

from features.firmware.service import FirmwareTaskService


class FirmwareTaskServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_task_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_file = Path(directory) / "upload.img"
            temporary_file.write_bytes(b"data")
            service = FirmwareTaskService()

            async def fail():
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                await service.run(
                    "task",
                    fail,
                    cleanup_paths=[temporary_file],
                )

            self.assertFalse(temporary_file.exists())
            self.assertEqual(service.status("task")["status"], "error")
