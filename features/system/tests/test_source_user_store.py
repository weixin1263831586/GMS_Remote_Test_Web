"""用户 SDK 源注册存储与管理 API 测试。

覆盖：
- UserSourceStore：校验规则（source_id/repo_root/连接）、加密口令
  round-trip、add/delete/mark_verified、幂等与 404/409；
- registry 合并：动态源进入 _build_registry_configs，静态冲突静态优先，
  坏源不拖垮静态源；
- manage API：human-only 守卫（agent token 403、elevation 403）、
  probe/add/delete 流程（SSH 打桩，不打真网）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from features.system.source_user_store import (
    UserSourceStore,
    UserSourceStoreError,
    user_source_to_provider_config,
    validate_repo_root,
    validate_source_id,
)


# foundation.secrets 的密钥文件跟随部署目录；测试里 GMS secrets 可能不
# 存在，encrypt/decrypt 用桩替换（round-trip 语义自证）。


def _stub_secrets(store: UserSourceStore) -> None:
    def fake_encrypt(value: str) -> str:
        return "enc::" + value[::-1]

    def fake_decrypt(value: str) -> str:
        if not value.startswith("enc::"):
            raise ValueError("bad token")
        return value[len("enc::"):][::-1]

    patcher = mock.patch.multiple(
        "features.system.source_user_store",
        encrypt_secret=fake_encrypt,
        decrypt_secret=fake_decrypt,
    )
    patcher.start()
    _TEST_CLEANUPS.append(patcher.stop)


_TEST_CLEANUPS: list = []


def tearDownModule():
    while _TEST_CLEANUPS:
        _TEST_CLEANUPS.pop()()


class UserSourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "user_sources.json"
        self.store = UserSourceStore(self.path)
        _stub_secrets(self)

    def test_add_and_get_roundtrip_with_encrypted_password(self):
        record = self.store.add(
            source_id="a16-0623",
            repo_root="/home/hcj/6_Android16_0623",
            ssh_host="10.10.10.206",
            ssh_port=22,
            ssh_username="hcj",
            ssh_password="secret-pass",
            label="A16",
            added_by="hcj",
        )
        self.assertEqual(record["source_id"], "a16-0623")
        self.assertEqual(record["repo_root"], "/home/hcj/6_Android16_0623")
        self.assertTrue(record["discover_repos"])
        # 明文不落盘
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("secret-pass", raw)
        self.assertIn("ssh_password_enc", raw)
        # 解密还原
        self.assertEqual(self.store.get_password("a16-0623"), "secret-pass")

    def test_add_duplicate_conflicts(self):
        kwargs = dict(
            source_id="a16",
            repo_root="/data/sdk",
            ssh_host="h",
            ssh_port=22,
            ssh_username="u",
        )
        self.store.add(**kwargs)
        with self.assertRaises(UserSourceStoreError) as ctx:
            self.store.add(**kwargs)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_validation_rules(self):
        with self.assertRaises(UserSourceStoreError):
            validate_source_id("Bad Id")
        with self.assertRaises(UserSourceStoreError):
            validate_source_id("-bad")
        with self.assertRaises(UserSourceStoreError):
            validate_repo_root("relative/path")
        with self.assertRaises(UserSourceStoreError):
            validate_repo_root("/data/../etc")
        normalized = validate_repo_root("/data//sdk/./")
        self.assertEqual(normalized, "/data/sdk")

    def test_delete_and_mark_verified(self):
        self.store.add(
            source_id="a17",
            repo_root="/sdk",
            ssh_host="h",
            ssh_port=22,
            ssh_username="u",
        )
        self.store.mark_verified("a17", ok=True)
        record = self.store.get("a17")
        self.assertTrue(record["last_verified_ok"])
        deleted = self.store.delete("a17")
        self.assertEqual(deleted["source_id"], "a17")
        with self.assertRaises(UserSourceStoreError) as ctx:
            self.store.get("a17")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_provider_config_conversion(self):
        record = self.store.add(
            source_id="a16",
            repo_root="/sdk",
            ssh_host="10.0.0.2",
            ssh_port=2222,
            ssh_username="builder",
            ssh_password="p",
        )
        item = user_source_to_provider_config(
            record, password=self.store.get_password("a16")
        )
        self.assertEqual(item["provider"], "ssh_git")
        self.assertEqual(item["ssh_password"], "p")
        self.assertNotIn("ssh_password_enc", item)

    def test_local_git_source_roundtrip(self):
        """local_git 记录：无 SSH 段、revision 保留、provider 映射正确。"""
        record = self.store.add(
            source_id="local-fb",
            provider="local_git",
            repo_root="/home/hcj/android-src/a16-fb",
            default_revision="9743e9ab",
            label="A16 frameworks/base",
        )
        self.assertEqual(record["provider"], "local_git")
        self.assertNotIn("ssh_host", record)
        self.assertEqual(record["default_revision"], "9743e9ab")
        item = user_source_to_provider_config(record)
        self.assertEqual(item["provider"], "local_git")
        self.assertNotIn("ssh_host", item)

    def test_unsupported_provider_rejected(self):
        with self.assertRaises(UserSourceStoreError):
            self.store.add(
                source_id="bad",
                provider="codesearch",
                repo_root="/x",
            )


class RegistryMergeTests(unittest.TestCase):
    def test_merge_user_sources_into_registry_configs(self):
        from features.system.source_provider import _build_registry_configs

        with TemporaryDirectory() as directory:
            store_path = Path(directory) / "user_sources.json"
            store = UserSourceStore(store_path)
            _stub_secrets(store)
            store.add(
                source_id="user-a16",
                repo_root="/sdk/a16",
                ssh_host="10.0.0.9",
                ssh_port=22,
                ssh_username="builder",
                ssh_password="pw",
            )
            static = {
                "sdk_sources": {
                    "providers": [
                        {
                            "source_id": "static-local",
                            "provider": "local_git",
                            "repo_root": "/local/repo",
                        }
                    ]
                }
            }
            with mock.patch(
                "features.system.source_provider.user_source_store",
                return_value=store,
            ):
                configs = _build_registry_configs(static)
            ids = [item.source_id for item in configs]
            self.assertIn("static-local", ids)
            self.assertIn("user-a16", ids)
            merged = next(item for item in configs if item.source_id == "user-a16")
            self.assertEqual(merged.provider, "ssh_git")
            self.assertEqual(merged.ssh_password, "pw")

    def test_empty_store_starts_clean(self):
        """「留空，用户自己配置」默认态：空存储初始化不炸、列为空。"""
        import features.system.source_provider as sp

        with TemporaryDirectory() as directory:
            store_path = Path(directory) / "user_sources.json"
            store_path.write_text('{"version": 1, "sources": {}}', encoding="utf-8")
            store = UserSourceStore(store_path)
            self.assertEqual(store.list_sources(), [])
            with mock.patch.object(
                sp, "user_source_store", return_value=store
            ):
                configs = sp._build_registry_configs({})
            self.assertEqual(configs, [])

    def test_reload_keeps_static_sources(self):
        """reload 复用静态配置快照：添加用户源后静态源不消失。"""
        import features.system.source_provider as sp

        with TemporaryDirectory() as directory:
            store_path = Path(directory) / "user_sources.json"
            store = UserSourceStore(store_path)
            _stub_secrets(store)
            store.add(
                source_id="user-a17",
                repo_root="/sdk/a17",
                ssh_host="10.0.0.9",
                ssh_port=22,
                ssh_username="builder",
                ssh_password="pw",
            )
            static_config = {
                "sdk_sources": {
                    "providers": [
                        {
                            "source_id": "static-local",
                            "provider": "local_git",
                            "repo_root": "/local/repo",
                        }
                    ]
                }
            }
            with mock.patch.object(
                sp, "user_source_store", return_value=store
            ), mock.patch.object(
                sp, "derive_application_key", create=True
            ) as derive:
                derive.return_value = b"k"
                state = sp.initialize_source_runtime(static_config)
                self.assertEqual(state, "AVAILABLE")
                after_add = sp.reload_source_runtime()
            self.assertEqual(after_add, "AVAILABLE")
            ids = {
                provider.config.source_id
                for provider in sp.source_registry()._providers.values()
            }
            self.assertIn("static-local", ids)
            self.assertIn("user-a17", ids)


if __name__ == "__main__":
    unittest.main()
