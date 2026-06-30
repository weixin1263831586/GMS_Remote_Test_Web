import unittest
from unittest.mock import Mock, patch

from foundation.processes import start_detached_process


class ProcessHelperTests(unittest.TestCase):
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
