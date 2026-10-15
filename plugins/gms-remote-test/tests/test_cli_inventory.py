"""Command discovery must work on Python 3.8 and modular runtime payloads."""

import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME = PACKAGE_ROOT / ("runtime" if (PACKAGE_ROOT / "runtime").is_dir() else "scripts")
MODULE = RUNTIME / "gms_agent" / "cli_inventory.py"


class TestCliInventory(unittest.TestCase):
    def load_with_legacy_set(self):
        # Python 3.8's builtin set cannot be subscripted in an annotation.
        # object has the same restriction, even on newer test interpreters.
        namespace = {"set": object}
        exec(compile(MODULE.read_text(), str(MODULE), "exec", dont_inherit=True), namespace)
        return namespace["cli_command_names"]

    def test_python38_import_and_modular_discovery(self):
        discover = self.load_with_legacy_set()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "gms-remote-test.sh"
            (root / "cli").mkdir()
            cli.write_text('source "$_gms_runtime_dir/cli/system.sh" || exit 7\n')
            (root / "cli" / "system.sh").write_text(
                "gms-rt-system-selfcheck() { :; }\n"
                "gms-rt-system-health() { :; }\n"
            )
            self.assertEqual(discover(cli), {
                "gms-rt-system-selfcheck", "gms-rt-system-health",
            })

    def test_shipped_runtime_discovers_client_commands(self):
        discover = self.load_with_legacy_set()
        commands = discover(RUNTIME / "gms-remote-test.sh")
        self.assertTrue({
            "gms-rt-system-selfcheck", "gms-rt-devices-diag",
            "gms-rt-agent-enroll", "gms-rt-system-update",
        }.issubset(commands))
