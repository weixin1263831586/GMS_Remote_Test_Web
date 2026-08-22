import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.sanitize_release_config import sanitize_file, sanitize_product_config
from scripts.verify_release_tree import verify_release_tree


class ReleasePackagingTests(unittest.TestCase):
    def test_tracked_product_config_uses_runtime_secret_placeholders(self):
        config = json.loads(Path("configs/config.json").read_text(encoding="utf-8"))

        self.assertEqual(config["ubuntu_pswd"], "${GMS_UBUNTU_PASSWORD:}")
        self.assertEqual(config["vnc_password"], "${GMS_VNC_PASSWORD:}")
        self.assertEqual(config["wifi"]["password"], "${GMS_WIFI_PASSWORD:}")
        self.assertEqual(
            config["ai_models"]["providers"]["glm_local"]["api_key"],
            "${GMS_LOCAL_AI_API_KEY:}",
        )
        self.assertEqual(
            config["ai_models"]["providers"]["zhipu"]["api_key"],
            "${GMS_ZHIPU_API_KEY:}",
        )

        runtime_example = json.loads(
            Path("configs/runtime.example.json").read_text(encoding="utf-8")
        )
        for key, value in runtime_example.items():
            if any(marker in key for marker in ("PASSWORD", "KEY", "TOKEN")):
                self.assertEqual(value, "", key)

    def test_install_script_declares_product_runtime_and_sensitive_exclusions(self):
        source = Path("install.sh").read_text(encoding="utf-8")

        for expected in (
            "--exclude '.certs/'",
            "--exclude '.gitignore'",
            "--exclude '.env.production'",
            "--exclude 'configs/env.production'",
            "--exclude 'configs/certs/'",
            "--exclude 'configs/runtime.json'",
            "--exclude 'configs/user_tools_data.json'",
            "--exclude 'configs/redmine_user_map.json'",
            "--exclude 'data/'",
            "--exclude '/*.png'",
            "--exclude '*.map'",
            "--exclude '/dist/'",
            "--exclude '/tools/gms-worker-native/target/'",
            "--exclude 'configs/config_runtime.json'",
            "--exclude 'docs/android-cli-ui-control-integration.md'",
            "--exclude 'docs/build-server-integration-assessment.md'",
            "--exclude 'docs/multi-host-cluster-implementation-plan.md'",
            "--exclude 'docs/refactor-parity-audit.md'",
            "--exclude 'tools/GMS-Host-Tools/gts-rockchip.json'",
            "Environment=GMS_ENV=production",
            'verify_release_tree.py" "${package_root}',
        ):
            self.assertIn(expected, source)

        for internal_document in (
            "docs/android-cli-ui-control-integration.md",
            "docs/build-server-integration-assessment.md",
            "docs/code-audit-2026-07.md",
            "docs/code-audit-2026-08-12.md",
            "docs/multi-host-cluster-implementation-plan.md",
            "docs/product-integration-cluster-audit-2026-07-15.md",
            "docs/product-release-checklist-2026-07-15.md",
            "docs/refactor-baseline.md",
            "docs/refactor-parity-audit.md",
            "docs/refactor-verification.md",
            "docs/wiki-knowledge-base-plan.md",
        ):
            self.assertEqual(source.count(f"--exclude '{internal_document}'"), 2)

        # EnvironmentFile was removed: the runtime environment JSON is loaded in-process
        # by bootstrap.env_loader, so systemd no longer needs it.
        self.assertNotIn("EnvironmentFile", source)
        self.assertNotIn("--exclude 'dist/'", source)

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required")
    def test_release_excludes_root_dist_but_keeps_prebuilt_native_dist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            (source / "dist").mkdir(parents=True)
            (source / "dist/archive.tar.gz").write_text("release", encoding="utf-8")
            native_dist = source / "tools/gms-worker-native/dist/x86_64"
            native_dist.mkdir(parents=True)
            (native_dist / "gms-process-inventory").write_text("binary", encoding="utf-8")
            native_target = source / "tools/gms-worker-native/target/release"
            native_target.mkdir(parents=True)
            (native_target / "build-object").write_text("object", encoding="utf-8")

            subprocess.run(
                [
                    "rsync", "-a",
                    "--exclude", "/dist/",
                    "--exclude", "/tools/gms-worker-native/target/",
                    f"{source}/", f"{destination}/",
                ],
                check=True,
            )

            self.assertFalse((destination / "dist/archive.tar.gz").exists())
            self.assertTrue(
                (destination / "tools/gms-worker-native/dist/x86_64/gms-process-inventory").is_file()
            )
            self.assertFalse((destination / "tools/gms-worker-native/target").exists())

    def test_installer_adds_limited_networkmanager_policy_for_service_user(self):
        installer = Path("install.sh").read_text(encoding="utf-8")
        policy = Path("scripts/install_networkmanager_policy.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("configure_networkmanager_policy", installer)
        self.assertIn(
            "org.freedesktop.NetworkManager.network-control",
            policy,
        )
        self.assertIn('subject.user == "${RUN_USER}"', policy)
        self.assertIn(
            "install -d -o root -g root -m 0755 /etc/polkit-1/rules.d",
            policy,
        )
        self.assertIn(
            "install -d -o root -g root -m 0755 "
            "/etc/polkit-1/localauthority/50-local.d",
            policy,
        )
        self.assertIn("Identity=unix-user:${RUN_USER}", policy)
        self.assertIn("ResultAny=yes", policy)
        self.assertIn("systemctl restart polkit.service", policy)
        self.assertIn(
            "--action-id org.freedesktop.NetworkManager.network-control",
            policy,
        )
        self.assertNotIn("systemctl reload polkit.service", policy)
        self.assertNotIn("settings.modify.system", policy)
        self.assertNotIn("sudoers", policy.lower())

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
        self.assertEqual(result["ai_models"]["providers"]["local"]["base_url"], "")
        self.assertEqual(result["redmine_auth"]["username"], "")
        self.assertEqual(result["gerrit_dashboard"]["ssh_host"], "")
        self.assertEqual(result["gerrit_dashboard"]["ssh_user"], "")
        self.assertEqual(result["product_branding"]["company_name"], "Organization")

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

    def test_customer_operational_configs_are_reset_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation = root / "automation_profiles.json"
            build = root / "build_servers.json"
            cluster = root / "cluster.json"
            automation.write_text('{"profiles":[{"id":"internal"}]}', encoding="utf-8")
            build.write_text(
                '{"servers":[{"host":"10.0.0.8"}],"templates":[{"command":"private"}]}',
                encoding="utf-8",
            )
            cluster.write_text(
                '{"enabled":true,"controller_url":"https://internal",'
                '"local_worker_id":"private-worker","default_max_jobs":8}',
                encoding="utf-8",
            )

            for path in (automation, build, cluster):
                sanitize_file(path)

            self.assertEqual(json.loads(automation.read_text())["profiles"], [])
            sanitized_build = json.loads(build.read_text())
            self.assertEqual(sanitized_build, {"servers": [], "templates": []})
            sanitized_cluster = json.loads(cluster.read_text())
            self.assertFalse(sanitized_cluster["enabled"])
            self.assertEqual(sanitized_cluster["controller_url"], "")
            self.assertEqual(sanitized_cluster["local_worker_id"], "ats-worker-controller")
            self.assertEqual(sanitized_cluster["default_max_jobs"], 8)

    def test_release_verifier_rejects_runtime_files_and_nested_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            (root / "configs/config_runtime.json").write_text("{}", encoding="utf-8")
            (root / "configs/user_tools_data.json").write_text("{}", encoding="utf-8")
            (root / "config.json").write_text(
                json.dumps({"provider": {"api_key": "leaked"}}),
                encoding="utf-8",
            )

            findings = verify_release_tree(root)

        self.assertTrue(any("runtime file" in item for item in findings))
        self.assertTrue(any("api_key" in item for item in findings))
        self.assertTrue(any("user_tools_data.json" in item for item in findings))

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

    def test_release_verifier_rejects_development_and_internal_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "web").mkdir()
            (root / ".gitignore").write_text("data/\n", encoding="utf-8")
            (root / "docs/code-audit-2026-08-12.md").write_text(
                "internal release policy",
                encoding="utf-8",
            )
            (root / "web/app.js.map").write_text("{}", encoding="utf-8")

            findings = verify_release_tree(root)

        self.assertTrue(any(".gitignore" in item for item in findings))
        self.assertTrue(any("internal document" in item for item in findings))
        self.assertTrue(any("source map" in item for item in findings))


if __name__ == "__main__":
    unittest.main()
