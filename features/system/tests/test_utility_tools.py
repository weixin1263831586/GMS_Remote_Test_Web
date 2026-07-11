import asyncio
import json
import unittest
from pathlib import Path

from fastapi import HTTPException

from features.system import assets, utility_tools_api


class UtilityToolsTests(unittest.TestCase):
    def test_manifest_uses_project_tools_directory(self):
        self.assertEqual(
            utility_tools_api.UTILITY_TOOLS_DIR,
            Path(__file__).resolve().parents[3] / 'tools',
        )
        self.assertTrue(
            (utility_tools_api.UTILITY_TOOLS_DIR / 'upgrade_tool').is_file()
        )

    def test_list_returns_existing_manifest_files(self):
        response = asyncio.run(utility_tools_api.list_utility_tools())
        payload = json.loads(response.body)

        names = {item['name'] for item in payload['files']}
        self.assertIn('upgrade_tool', names)
        self.assertIn('misc.img', names)

    def test_non_manifest_file_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            utility_tools_api._resolve_allowed_utility_tool('refactor_visual_parity.py')

        self.assertEqual(raised.exception.status_code, 403)

    def test_remote_file_path_is_shell_quoted(self):
        command = assets._remote_list_command("/tmp/a' ; touch /tmp/injected; '")

        self.assertEqual(
            command,
            "ls -la -- '/tmp/a'\"'\"' ; touch /tmp/injected; '\"'\"'' 2>/dev/null",
        )


if __name__ == '__main__':
    unittest.main()
