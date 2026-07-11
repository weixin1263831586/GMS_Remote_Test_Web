import unittest

from features.test_execution.process_control import (
    build_process_group_id,
    find_arg_pgid_command,
    find_env_pgid_command,
    kill_pid_tree_commands,
    parse_pid_lines,
)


class ProcessControlTests(unittest.TestCase):
    def test_build_process_group_id_sanitizes_client_id(self):
        self.assertEqual(
            build_process_group_id("user@127.0.0.1; rm -rf /", 123),
            "gms_test_user_127.0.0.1_rm_-rf_123",
        )

    def test_parse_pid_lines_keeps_only_safe_pids(self):
        self.assertEqual(parse_pid_lines("1\n42\nabc\n  99  \n0\n12;rm\n"), ["42", "99"])

    def test_kill_pid_tree_commands_rejects_invalid_pid(self):
        self.assertEqual(kill_pid_tree_commands("abc"), [])
        self.assertEqual(kill_pid_tree_commands("1"), [])
        self.assertEqual(
            kill_pid_tree_commands("42"),
            ["kill -9 42 2>/dev/null", "pkill -9 -P 42 2>/dev/null"],
        )

    def test_find_commands_quote_process_group_id(self):
        env_cmd = find_env_pgid_command("group with spaces")
        arg_cmd = find_arg_pgid_command("group with spaces")

        self.assertIn("grep -F --", env_cmd)
        self.assertIn("'GMS_TEST_PGID=group with spaces'", env_cmd)
        self.assertIn("grep -- '--pgid group with spaces'", arg_cmd)

if __name__ == "__main__":
    unittest.main()
