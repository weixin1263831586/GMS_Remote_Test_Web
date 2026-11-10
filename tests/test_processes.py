import os
import sys
import tempfile
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
