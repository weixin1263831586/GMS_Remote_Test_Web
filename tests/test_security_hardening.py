import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.check_source_secrets import find_literal_secret_paths, scan_tracked_files
from scripts.sanitize_tracked_config import sanitize_config


class SecurityHardeningTests(unittest.TestCase):
    def _tracked_secret_findings(self, relative: str, content: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'add', relative], cwd=root, check=True)
            return scan_tracked_files(root)

    def test_literal_secret_detector_accepts_placeholders(self):
        payload = {
            'password': '${GMS_PASSWORD:}',
            'provider': {'api_key': '${GMS_API_KEY:}'},
            'empty_secret': '',
        }
        self.assertEqual(find_literal_secret_paths(payload), [])

    def test_literal_secret_detector_rejects_values_without_printing_them(self):
        payload = {
            'password': 'literal-password',
            'provider': {'api_key': 'literal-api-key'},
        }
        self.assertEqual(
            find_literal_secret_paths(payload),
            ['password', 'provider.api_key'],
        )

    def test_tracked_json_runs_content_marker_scan_after_key_scan(self):
        token = 'gh' + 'p_' + ('A' * 32)
        findings = self._tracked_secret_findings(
            'metadata.json', json.dumps({'opaque_value': token})
        )
        self.assertTrue(any('github_token' in item for item in findings))

    def test_tracked_test_fixture_is_not_blanket_exempt(self):
        token = 'AI' + 'za' + ('A' * 32)
        findings = self._tracked_secret_findings(
            'tests/fixture.txt', f'credential={token}'
        )
        self.assertTrue(any('google_api_key' in item for item in findings))

    def test_sanitizer_migrates_literals_to_runtime_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / 'config.json'
            runtime_path = root / 'runtime.json'
            config_path.write_text(
                json.dumps(
                    {
                        'ubuntu_pswd': 'ubuntu-secret',
                        'wifi': {'password': 'wifi-secret'},
                        'ai_models': {
                            'providers': {
                                'glm_local': {'api_key': 'local-key'},
                                'custom-provider': {'api_key': 'custom-key'},
                            }
                        },
                    }
                ),
                encoding='utf-8',
            )

            migrated, sanitized = sanitize_config(config_path, runtime_path)

            self.assertEqual(migrated, 4)
            self.assertEqual(sanitized, 4)
            config = json.loads(config_path.read_text(encoding='utf-8'))
            runtime = json.loads(runtime_path.read_text(encoding='utf-8'))
            self.assertEqual(config['ubuntu_pswd'], '${GMS_UBUNTU_PASSWORD:}')
            self.assertEqual(config['wifi']['password'], '${GMS_WIFI_PASSWORD:}')
            self.assertEqual(
                config['ai_models']['providers']['glm_local']['api_key'],
                '${GMS_LOCAL_AI_API_KEY:}',
            )
            self.assertEqual(
                config['ai_models']['providers']['custom-provider']['api_key'],
                '${GMS_AI_CUSTOM_PROVIDER_API_KEY:}',
            )
            self.assertEqual(runtime['GMS_UBUNTU_PASSWORD'], 'ubuntu-secret')
            self.assertEqual(runtime['GMS_WIFI_PASSWORD'], 'wifi-secret')
            self.assertEqual(runtime['GMS_LOCAL_AI_API_KEY'], 'local-key')
            self.assertEqual(runtime['GMS_AI_CUSTOM_PROVIDER_API_KEY'], 'custom-key')
            self.assertEqual(stat.S_IMODE(runtime_path.stat().st_mode), 0o600)

    def test_modal_helper_does_not_interpolate_dynamic_text_into_html(self):
        source = Path('web/static/js/modal.js').read_text(encoding='utf-8')
        self.assertNotIn('${title}', source)
        self.assertNotIn('${loadingMessage}', source)
        self.assertIn(".textContent = String(title ?? '')", source)
        self.assertIn("addEventListener('click'", source)

    def test_static_assets_are_revalidated_until_fingerprinted(self):
        source = Path('bootstrap/application.py').read_text(encoding='utf-8')
        self.assertIn('public, max-age=300, must-revalidate', source)
        self.assertNotIn('public, max-age=86400, immutable', source)

    def test_usb_dispatcher_is_event_driven(self):
        source = Path('bootstrap/lifecycle.py').read_text(encoding='utf-8')
        self.assertIn('await app.state.usb_event_queue.get()', source)
        self.assertIn('loop.call_soon_threadsafe(enqueue_usb_event, event)', source)
        self.assertNotIn('queue.Empty', source)


if __name__ == '__main__':
    unittest.main()
