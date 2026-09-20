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

    def test_manifest_maps_tool_ids_to_project_script_paths(self):
        gerrit = utility_tools_api.UTILITY_TOOL_MANIFEST['gerrit-patch']
        self.assertEqual(
            gerrit['path'],
            'scripts/utilities/gerrit_patch_export_and_apply.sh',
        )
        self.assertEqual(
            gerrit['download_name'],
            'gerrit_patch_export_and_apply_tool.sh',
        )
        # 浏览器只拿稳定 tool_id，download_name 与真实路径只在后端解析。
        self.assertTrue(
            (
                utility_tools_api.UTILITY_TOOLS_DIR / gerrit['path']
            ).is_file()
        )

    def test_list_returns_manifest_entries_with_stable_ids(self):
        response = asyncio.run(utility_tools_api.list_utility_tools())
        payload = json.loads(response.body)

        ids = {item['tool_id'] for item in payload['files']}
        self.assertIn('upgrade-tool', ids)
        self.assertIn('misc-img', ids)
        self.assertIn('gerrit-patch', ids)
        names = {item['name'] for item in payload['files']}
        self.assertIn('upgrade_tool', names)
        self.assertIn('misc.img', names)
        # 清单条目绝不泄露 tools/ 下真实路径
        self.assertNotIn('scripts/utilities/gerrit_patch_export_and_apply.sh', names)

    def test_non_manifest_tool_id_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            utility_tools_api._resolve_allowed_utility_tool('unknown-tool')

        self.assertEqual(raised.exception.status_code, 403)

    def test_download_resolves_legacy_file_names(self):
        self.assertEqual(
            utility_tools_api._resolve_tool_id(
                'gerrit_patch_export_and_apply_tool.sh'
            ),
            'gerrit-patch',
        )
        self.assertEqual(
            utility_tools_api._resolve_tool_id('gerrit-patch'),
            'gerrit-patch',
        )

    def test_remote_file_path_is_shell_quoted(self):
        command = assets._remote_list_command("/tmp/a' ; touch /tmp/injected; '")

        self.assertEqual(
            command,
            "ls -la -- '/tmp/a'\"'\"' ; touch /tmp/injected; '\"'\"'' 2>/dev/null",
        )


if __name__ == '__main__':
    unittest.main()
