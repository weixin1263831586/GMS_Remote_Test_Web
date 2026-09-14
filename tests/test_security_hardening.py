import json
import re
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

    def test_release_installer_and_agent_runtime_have_no_tls_downgrade(self):
        """供应链门禁:release installer / agent runtime 不得出现静默 TLS 降级。

        curl -k / --insecure / wget --no-check-certificate / verify=False /
        ssl=False 只允许出现在显式 opt-in 守护分支，或受控 TOFU 首次
        获取 Controller CA 的分支；不得用于后续 manifest/包下载。
        """
        guarded = (
            'GMS_INSTALL_ALLOW_INSECURE',
            'GMS_INSTALL_INSECURE',
        )
        scanned = [
            Path('features/system/agent_package_registry.py'),
            Path('agent/gms-remote-test/runtime/gms-agent'),
            Path('agent/gms-remote-test/runtime/gms_agent/package_manager.py'),
        ]
        # 用正则而非固定子串,覆盖 -k / -ksSL / -kfsSL / --insecure 等变体,
        # 以及 wget 的 --no-check-certificate 与 Python 侧的显式关闭。
        downgrade_patterns = (
            re.compile(r'\bcurl\b[^\n]*\s(?:-[a-zA-Z]*k[a-zA-Z]*|--insecure)\b'),
            re.compile(r'--no-check-certificate\b'),
            re.compile(r'curl_insecure\s*=\s*True'),
            re.compile(r'\bssl\s*=\s*False'),
            re.compile(r'\bverify\s*=\s*False'),
        )

        for path in scanned:
            self.assertTrue(path.exists(), path)
            lines = path.read_text(encoding='utf-8').splitlines()
            for index, line in enumerate(lines):
                if not any(pattern.search(line) for pattern in downgrade_patterns):
                    continue
                # install.sh 的两处“如何首次取脚本”仅为返回脚本内注释和
                # Python docstring，不是本模块执行的下载命令。
                if "/api/agent/install.sh" in line and "paircode" in line:
                    continue
                # 按真实行号取上文窗口;避免 splitlines().index() 对重复行
                # 只返回首个索引、让未守护的降级行借用别处的守护上下文。
                context = '\n'.join(lines[max(0, index - 12): index + 1])
                tofu_ca_fetch = (
                    "/api/agent/ca.crt" in line and "TOFU" in context
                )
                self.assertTrue(
                    tofu_ca_fetch or any(guard in context for guard in guarded),
                    f'{path}:{index + 1}: TLS 降级行缺少显式开关守护: {line.strip()}',
                )
        # verify=False 属于硬禁止:任何位置都不允许。
        for path in scanned:
            self.assertNotIn(
                'verify=False',
                path.read_text(encoding='utf-8'),
                path,
            )

    def test_tls_gate_detects_bypass_samples(self):
        """门禁自检:未守护的降级样例必须被判为违规,守护样例必须放行。

        防止正则/窗口逻辑回退成永远通过的橡皮图章。
        """
        downgrade_patterns = (
            re.compile(r'\bcurl\b[^\n]*\s(?:-[a-zA-Z]*k[a-zA-Z]*|--insecure)\b'),
            re.compile(r'--no-check-certificate\b'),
            re.compile(r'curl_insecure\s*=\s*True'),
            re.compile(r'\bssl\s*=\s*False'),
            re.compile(r'\bverify\s*=\s*False'),
        )

        def flags(source: str) -> list[int]:
            return [
                index
                for index, line in enumerate(source.splitlines())
                if any(pattern.search(line) for pattern in downgrade_patterns)
            ]

        unguarded = 'curl -kfsSL https://example.com/x.sh\n'
        self.assertEqual(len(flags(unguarded)), 1, 'curl -kfsSL 应命中')
        self.assertEqual(len(flags('curl -k https://x/y\n')), 1, 'curl -k 应命中')
        self.assertEqual(
            len(flags('wget --no-check-certificate https://x\n')), 1,
            'wget --no-check-certificate 应命中',
        )
        self.assertEqual(len(flags('curl -fsSL https://x/y\n')), 0, '严格 curl 不应命中')
        self.assertEqual(
            len(flags('if [ -n "$GMS_INSTALL_INSECURE" ]; then\n  curl -fsSL -k u\nfi\n')),
            1,
            '守护分支内的 -k 仍应命中(交由上文窗口判定守护)',
        )


if __name__ == '__main__':
    unittest.main()
