import json
import ssl
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization


class GerritConfigTests(unittest.TestCase):
    def setUp(self):
        self.secret_env = patch.dict(
            "os.environ",
            {"GMS_SECRET_KEY": Fernet.generate_key().decode("ascii")},
        )
        self.secret_env.start()

    def tearDown(self):
        self.secret_env.stop()

    def test_rest_tls_defaults_to_verified_with_ca_support(self):
        """评审 P1：Basic Auth 出站必须默认校验 TLS；自签 CA 走 rest_ca_cert。"""
        from features.gerrit.config import (
            DEFAULT_GERRIT_DASHBOARD,
            denormalize_gerrit_dashboard_config,
            normalize_gerrit_dashboard_config,
        )
        from features.gerrit.service import _rest_ssl

        # 默认开启校验（历史默认 False 是 MITM 面）
        self.assertTrue(DEFAULT_GERRIT_DASHBOARD["rest_verify_ssl"])
        self.assertEqual(DEFAULT_GERRIT_DASHBOARD["rest_ca_cert"], "")

        # 未配置 → 校验开启，无 CA 时用系统信任库
        cfg = normalize_gerrit_dashboard_config({})
        self.assertTrue(cfg["rest_verify_ssl"])
        self.assertEqual(cfg["rest_ca_cert"], "")
        self.assertIs(_rest_ssl(cfg), True)

        # 配置私有 CA → context 携带 cafile（用真实自签证书，OpenSSL 拒绝坏 PEM）
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gms-test-ca")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        with TemporaryDirectory() as tmp:
            ca = Path(tmp) / "internal-ca.crt"
            ca.write_text(cert.public_bytes(serialization.Encoding.PEM).decode())
            cfg_ca = normalize_gerrit_dashboard_config({"rest_ca_cert": str(ca)})
            context = _rest_ssl(cfg_ca)
            self.assertIsInstance(context, ssl.SSLContext)
            # denormalize 往返不丢 rest_ca_cert
            raw = denormalize_gerrit_dashboard_config(cfg_ca)
            self.assertEqual(raw["rest_ca_cert"], str(ca))

        # 显式关闭（仅旧部署兼容路径）→ 仍然显式 False，而不是静默回退
        legacy = normalize_gerrit_dashboard_config({"rest_verify_ssl": False})
        self.assertFalse(legacy["rest_verify_ssl"])
        self.assertIs(_rest_ssl(legacy), False)

    def test_profile_update_preserves_unrelated_profiles(self):
        from features.gerrit.config import (
            add_gerrit_personal_profile,
            normalize_gerrit_dashboard_config,
        )

        current = normalize_gerrit_dashboard_config(
            {
                "dashboard_profiles": [
                    {"id": "open", "name": "Open", "query": "status:open"},
                    {"id": "merged", "name": "Merged", "query": "status:merged"},
                ],
                "department_profiles": [
                    {"id": "platform", "name": "Platform", "owners": []},
                ],
                "personal_profiles": [],
            }
        )

        updated = add_gerrit_personal_profile(
            current,
            "Alice",
            "alice@example.com",
            department_id="platform",
        )

        self.assertEqual(
            [profile["id"] for profile in updated["dashboard_profiles"]],
            ["open", "merged"],
        )
        self.assertEqual(updated["department_profiles"][0]["id"], "platform")
        self.assertIn(
            "alice@example.com",
            updated["department_profiles"][0]["owners"],
        )

    def test_redmine_user_sync_builds_department_and_personal_profiles(self):
        from features.gerrit.config import (
            normalize_gerrit_dashboard_config,
            sync_gerrit_members_from_redmine_users,
        )

        current = normalize_gerrit_dashboard_config(
            {
                "department_profiles": [],
                "personal_profiles": [],
            }
        )
        updated = sync_gerrit_members_from_redmine_users(
            current,
            [
                {
                    "name": "Alice",
                    "email": "alice@example.com",
                    "department_id": "platform",
                    "department": "Platform",
                }
            ],
        )

        department = next(
            profile
            for profile in updated["department_profiles"]
            if profile["id"] == "platform"
        )
        personal = next(
            profile
            for profile in updated["personal_profiles"]
            if profile["owner"] == "alice@example.com"
        )
        self.assertEqual(department["owners"], ["alice@example.com"])
        self.assertEqual(personal["department_id"], "platform")

    def test_redmine_user_sync_overwrites_stale_personal_name(self):
        # Redmine 用户映射是姓名的权威来源。
        from features.gerrit.config import (
            normalize_gerrit_dashboard_config,
            sync_gerrit_members_from_redmine_users,
        )

        current = normalize_gerrit_dashboard_config(
            {
                "department_profiles": [],
                "personal_profiles": [
                    {"owner": "alice@example.com", "name": "alice"}
                ],
            }
        )
        updated = sync_gerrit_members_from_redmine_users(
            current,
            [
                {
                    "name": "爱丽丝",
                    "email": "alice@example.com",
                    "department_id": "platform",
                    "department": "Platform",
                }
            ],
        )
        personal = next(
            profile
            for profile in updated["personal_profiles"]
            if profile["owner"] == "alice@example.com"
        )
        self.assertEqual(personal["name"], "爱丽丝")
        self.assertEqual(personal["department"], "Platform")

    def test_feature_gerrit_config_save_preserves_other_runtime_sections(self):
        from features.gerrit.settings import GerritConfig

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foundation").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(
                json.dumps({"gerrit_dashboard": {"base_url": "https://old.example.com"}}),
                encoding="utf-8",
            )
            (configs / "config_runtime.json").write_text(
                json.dumps({"redmine_auth": {"username": "u"}, "sidebar_order": ["test"]}),
                encoding="utf-8",
            )

            manager = GerritConfig(project_root=root)

            self.assertTrue(manager.save_gerrit_dashboard_config({
                "base_url": "https://10.10.10.29/",
                "department_profiles": [{"id": "sys", "name": "系统部", "owners": ["dev@example.com"]}],
            }))

            runtime = json.loads((configs / "config_runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime["redmine_auth"]["username"], "u")
            self.assertEqual(runtime["sidebar_order"], ["test"])
            self.assertEqual(runtime["gerrit_dashboard"]["base_url"], "https://10.10.10.29")

    def test_gerrit_request_config_is_isolated_per_platform_owner(self):
        import features.gerrit.api as gerrit_api
        from features.auth import CurrentUser
        from features.gerrit.settings import GerritConfig

        def request_for(user_id):
            return SimpleNamespace(
                state=SimpleNamespace(current_user=CurrentUser(user_id, user_id, "user")),
                cookies={},
            )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "configs").mkdir()
            (root / "foundation").mkdir()
            # per-owner 配置路径由 project_root 推导（canonical 在
            # configs/secrets/<feature>/by_user/），无需 patch settings。
            with patch.object(
                gerrit_api, "config_manager", GerritConfig(root)
            ):
                alice_cfg = gerrit_api._config_for_request(request_for("alice-isolated"))
                bob_cfg = gerrit_api._config_for_request(request_for("bob-isolated"))
                self.assertNotEqual(
                    str(alice_cfg.runtime_config_path),
                    str(bob_cfg.runtime_config_path),
                )
                self.assertEqual(
                    Path(alice_cfg.runtime_config_path),
                    root / "configs/secrets/gerrit/by_user/alice-isolated/config_runtime.json",
                )
                self.assertEqual(
                    Path(bob_cfg.runtime_config_path),
                    root / "configs/secrets/gerrit/by_user/bob-isolated/config_runtime.json",
                )

    def test_gerrit_department_config_is_derived_from_redmine_user_map_when_runtime_config_empty(self):
        import features.gerrit.api as gerrit_api
        from features.auth import CurrentUser

        class FakeManager:
            def get_gerrit_dashboard_config(self):
                return {}

            def for_owner(self, owner_id):
                return self

        request = SimpleNamespace(
            state=SimpleNamespace(current_user=CurrentUser("alice", "alice", "user")),
            cookies={},
        )
        old_manager = gerrit_api.config_manager
        try:
            gerrit_api.config_manager = FakeManager()
            # 使用当前用户的 Redmine 用户映射。
            with patch.object(gerrit_api, "load_redmine_user_map_for_owner", return_value=[
                {
                    "name": "Alice",
                    "email": "alice@example.com",
                    "department_id": "system-2",
                    "department": "系统二部",
                },
                {
                    "name": "Bob",
                    "email": "bob@example.com",
                    "department_id": "system-2",
                    "department": "系统二部",
                },
            ]):
                cfg = gerrit_api._dashboard_config_for_request(request)

            system_2 = next(profile for profile in cfg["department_profiles"] if profile["id"] == "system-2")
            self.assertEqual(system_2["owners"], ["alice@example.com", "bob@example.com"])
        finally:
            gerrit_api.config_manager = old_manager


if __name__ == "__main__":
    unittest.main()
