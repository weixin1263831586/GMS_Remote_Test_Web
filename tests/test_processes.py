import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from foundation.processes import run_local_command, start_detached_process


class ProcessHelperTests(unittest.TestCase):
    def test_local_command_isolates_environment_and_working_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GMS_PROCESS_PARENT_MARKER": "parent"}):
            result = run_local_command([
                sys.executable, "-c",
                "import os; print(os.getcwd()); print(os.getenv('GMS_PROCESS_FIXTURE')); print(os.getenv('GMS_PROCESS_PARENT_MARKER'))",
            ], env={"GMS_PROCESS_FIXTURE": "isolated"}, cwd=directory)
            self.assertTrue(result.ok, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [directory, "isolated", "None"])

    def test_cancel_event_kills_running_process_and_reports_cancelled(self):
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        started = time.monotonic()
        result = run_local_command(
            [sys.executable, "-c", "print('child started', flush=True); import time; time.sleep(300)"],
            timeout=120, cancel_event=cancel,
        )
        elapsed = time.monotonic() - started
        self.assertFalse(result.ok)
        self.assertEqual(result.stderr, "Command cancelled")
        # 取消事件触发后 ~1s 内中止，而不是等满 timeout。
        self.assertLess(elapsed, 10)

    def test_cancel_event_set_before_start_still_reports_cancelled(self):
        # 子进程睡过首个 1s 轮询窗口，保证取消路径确定触发（Popen→killpg）。
        cancel = threading.Event()
        cancel.set()
        result = run_local_command([sys.executable, "-c", "import time; time.sleep(30)"], cancel_event=cancel)
        self.assertFalse(result.ok)
        self.assertEqual(result.stderr, "Command cancelled")

    def test_cancel_event_unset_keeps_plain_communicate_contract(self):
        result = run_local_command([sys.executable, "-c", "print('done')"], timeout=30,
                                   cancel_event=threading.Event())
        self.assertTrue(result.ok)
        self.assertIn("done", result.stdout)

    def test_start_detached_process_starts_reaper_thread(self):
        process = Mock()
        process.pid = 1234

        with (
            patch("foundation.processes.subprocess.Popen", return_value=process) as popen,
            patch("foundation.processes.threading.Thread") as thread_cls,
        ):
            returned = start_detached_process(["websockify", "6080"], name="websockify_6080")

        self.assertIs(returned, process)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
