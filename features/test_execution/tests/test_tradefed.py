import os
import tempfile
import unittest

from features.test_execution.tradefed import (
    find_tradefed_binary,
    find_tradefed_binary_local,
    sanitize_tradefed_console_command,
)
from foundation.command_result import CommandResult


class FakeRuntimeSshManager:
    def __init__(self):
        self.commands = []

    def execute_command(self, _ssh, command, timeout=None):
        self.commands.append(command)
        return CommandResult(stdout="/suite/tools/cts-tradefed\n", stderr="", code=0)


class TradefedTests(unittest.TestCase):
    def test_find_tradefed_binary_quotes_suite_path(self):
        import features.test_execution.tradefed as tradefed

        old_manager = tradefed.runtime.ssh_manager
        fake_manager = FakeRuntimeSshManager()
        tradefed.runtime.ssh_manager = fake_manager
        try:
            result = find_tradefed_binary(object(), "/tmp/suite with 'quote'/tools")
        finally:
            tradefed.runtime.ssh_manager = old_manager

        self.assertEqual(result, "/suite/tools/cts-tradefed")
        self.assertIn("find '/tmp/suite with '\"'\"'quote'\"'\"'/tools'", fake_manager.commands[0])

    def test_sanitize_tradefed_console_command_rejects_multiline(self):
        self.assertEqual(sanitize_tradefed_console_command(" list results "), "list results")
        with self.assertRaises(ValueError):
            sanitize_tradefed_console_command("list results\nexit")

    def test_find_tradefed_binary_local_requires_executable_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            disabled = os.path.join(tmp, "cts-tradefed")
            enabled = os.path.join(tmp, "vts-tradefed")
            for path in (disabled, enabled):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("#!/bin/sh\n")
            os.chmod(enabled, 0o755)
            self.assertEqual(find_tradefed_binary_local(tmp), enabled)


if __name__ == "__main__":
    unittest.main()
