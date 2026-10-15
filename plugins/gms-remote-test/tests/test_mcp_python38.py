"""Tool registration must work on build servers still running Python 3.8."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
spec = importlib.util.spec_from_file_location("mcp_python38_test_server", RUNTIME / "mcp_server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class Python38String(str):
    def removeprefix(self, *_args):
        raise AttributeError("str.removeprefix is unavailable on Python 3.8")


class TestPython38ToolRegistration(unittest.TestCase):
    def test_tool_safety_lookup_does_not_require_python39_string_methods(self):
        with patch.object(server, "_command_safety", return_value=None) as safety:
            tool = server._decorate_tool({"name": Python38String("gms_rt_apk_deploy")})
        safety.assert_called_once_with("gms-rt-apk-deploy")
        self.assertFalse(tool["annotations"]["readOnlyHint"])
