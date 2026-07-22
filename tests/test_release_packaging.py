import json
import tempfile
import unittest
from pathlib import Path

from scripts.sanitize_release_config import sanitize_file, sanitize_product_config
from scripts.verify_release_tree import verify_release_tree


class ReleasePackagingTests(unittest.TestCase):
    def test_install_script_declares_product_runtime_and_sensitive_exclusions(self):
        source = Path("install.sh").read_text(encoding="utf-8")

        for expected in (
            "--exclude '.certs/'",
            "--exclude '.env.production'",
            "--exclude 'configs/env.production'",
            "--exclude 'configs/certs/'",
            "--exclude 'configs/runtime.json'",
            "--exclude 'data/'",
            "--exclude 'configs/config_runtime.json'",
            "--exclude 'tools/GMS-Host-Tools/gts-rockchip.json'",
            "Environment=GMS_ENV=production",
            'verify_release_tree.py" "${package_root}',
        ):
            self.assertIn(expected, source)

        # EnvironmentFile was removed: runtime.json is now loaded in-process
        # by bootstrap.env_loader, so systemd no longer needs it.
        self.assertNotIn("EnvironmentFile", source)

    def test_product_config_scrubs_nested_secrets_and_source_identity(self):
        source = {
            "ubuntu_user": "builder",
            "ubuntu_host": "192.0.2.10",
            "ubuntu_pswd": "secret",
            "local_server": "builder@192.0.2.10",
            "private_key_path": "/home/builder/.ssh/id_rsa",
            "client_hosts": {"192.0.2.20": "builder"},
            "client_ssh_credentials": [{"password": "secret"}],
            "device_groups": [{"id": "private"}],
            "ai_models": {"providers": {"local": {"api_key": "key", "base_url": "https://ai.example"}}},
            "redmine_auth": {"username": "builder", "encrypted_password": "cipher"},
            "gerrit_dashboard": {
                "ssh_host": "gerrit.example",
                "ssh_user": "builder",
                "rest_password": "secret",
            },
        }

        result = sanitize_product_config(source)

        self.assertEqual(result["ubuntu_user"], "")
        self.assertEqual(result["ubuntu_host"], "127.0.0.1")
        self.assertEqual(result["client_ssh_credentials"], [])
        self.assertEqual(result["device_groups"], [])
        self.assertEqual(result["ai_models"]["providers"]["local"]["api_key"], "")
        self.assertEqual(result["ai_models"]["providers"]["local"]["base_url"], "https://ai.example")
        self.assertEqual(result["redmine_auth"]["username"], "")
        self.assertEqual(result["gerrit_dashboard"]["ssh_host"], "gerrit.example")
        self.assertEqual(result["gerrit_dashboard"]["ssh_user"], "")

    def test_skill_config_keeps_search_settings_but_removes_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "skill-config.json"
            path.write_text(
                json.dumps({"base_url": "https://search.example", "token": "secret", "default_limit": 10}),
                encoding="utf-8",
            )

            sanitize_file(path)
            result = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(result["base_url"], "https://search.example")
        self.assertEqual(result["token"], "")
        self.assertEqual(result["default_limit"], 10)

    def test_release_verifier_rejects_runtime_files_and_nested_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            (root / "configs/config_runtime.json").write_text("{}", encoding="utf-8")
            (root / "config.json").write_text(
                json.dumps({"provider": {"api_key": "leaked"}}),
                encoding="utf-8",
            )

            findings = verify_release_tree(root)

        self.assertTrue(any("runtime file" in item for item in findings))
        self.assertTrue(any("api_key" in item for item in findings))

    def test_release_verifier_accepts_sanitized_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            (root / "configs/config.json").write_text(
                json.dumps({"password": "", "api_key": ""}),
                encoding="utf-8",
            )
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "validator.py").write_text(
                'BEGIN = "-----BEGIN PRIVATE KEY-----"\n'
                'END = "-----END PRIVATE KEY-----"\n',
                encoding="utf-8",
            )

            self.assertEqual(verify_release_tree(root), [])

    def test_release_verifier_detects_pem_block_without_flagging_marker_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "leaked.pem").write_text(
                "-----BEGIN PRIVATE KEY-----\n"
                + ("A" * 96)
                + "\n-----END PRIVATE KEY-----\n",
                encoding="utf-8",
            )

            findings = verify_release_tree(root)

        self.assertTrue(any("private key material" in item for item in findings))


if __name__ == "__main__":
    unittest.main()
