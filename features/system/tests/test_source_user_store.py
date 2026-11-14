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

    def test_corrupt_store_blocks_writes_and_backs_up(self):
        """损坏的存储文件绝不能被空数据覆盖：读失败、写阻断、留备份。"""
        self.path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(UserSourceStoreError) as ctx:
            self.store.list_sources()
        self.assertEqual(ctx.exception.status_code, 503)
        # add 走同一加载路径，必须同样失败而不是静默重置。
        with self.assertRaises(UserSourceStoreError):
            self.store.add(
                source_id="a16", repo_root="/data/sdk",
                ssh_host="h", ssh_port=22, ssh_username="u",
            )
        # 原文件原样保留 + .corrupt 备份存在。
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not valid json")
        backup = self.path.with_suffix(self.path.suffix + ".corrupt")
        self.assertEqual(backup.read_text(encoding="utf-8"), "{not valid json")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        # 修复后（恢复合法内容）可正常使用。
        self.path.write_text('{"version": 1, "sources": {}}', encoding="utf-8")
        self.assertEqual(self.store.list_sources(), [])

    def test_structurally_invalid_store_blocks_writes(self):
        """语法合法但结构错误的存储同样阻断写入（2026-11-14 审核意见）。

        旧实现把 ``sources: []``、缺 version 等结构损坏当空存储返回，
        后续 add 用空数据覆盖原文件、已注册源静默丢失。
        """
        cases = [
            '{"version": 1, "sources": []}',        # sources 是数组
            '{"sources": {}}',                       # 缺 version
            '{"version": "1", "sources": {}}',       # version 类型错
            '{"version": 2, "sources": {}}',         # 版本不识别
            '{"version": 1}',                        # 缺 sources
            '[]',                                    # 顶层不是对象
            '{"version": 1, "sources": {"a": []}}',  # 记录不是对象
            '{"version": 1, "sources": {"a": {"source_id": "b"}}}',  # 键不一致
        ]
        for content in cases:
            with self.subTest(content=content):
                backup = self.path.with_suffix(self.path.suffix + ".corrupt")
                backup.unlink(missing_ok=True)
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(UserSourceStoreError) as ctx:
                    self.store.list_sources()
                self.assertEqual(ctx.exception.status_code, 503)
                # add 走同一加载路径，必须同样失败而不是静默重置。
                with self.assertRaises(UserSourceStoreError):
                    self.store.add(
                        source_id="a16", repo_root="/data/sdk",
                        ssh_host="h", ssh_port=22, ssh_username="u",
                    )
                # 原文件原样保留。
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)
        # 修复后可正常使用。
        self.path.write_text('{"version": 1, "sources": {}}', encoding="utf-8")
        self.assertEqual(self.store.list_sources(), [])

    def test_validation_rules(self):
        with self.assertRaises(UserSourceStoreError):
            validate_source_id("Bad Id")
        with self.assertRaises(UserSourceStoreError):
            validate_source_id("-bad")
        # 大小写保持：目录名风格的混合大小写 source_id 合法且原样保留。
        self.assertEqual(validate_source_id("7_Android17_1017"), "7_Android17_1017")
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
    def setUp(self):
        """快照 source_provider 模块级全局：registry 相关测试会改写它们，
        不恢复会污染后续测试（SdkApiTests 读到残留的 static 配置）。"""
        import features.system.source_provider as sp

        self._sp = sp
        self._saved = (
            sp._REGISTRY, sp._REGISTRY_SECRET, sp._LAST_STATIC_CONFIG,
            sp._USER_STORE, sp._REGISTRY_BUILT_AT,
        )
        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        import features.system.source_provider as sp

        (
            sp._REGISTRY, sp._REGISTRY_SECRET, sp._LAST_STATIC_CONFIG,
            sp._USER_STORE, sp._REGISTRY_BUILT_AT,
        ) = self._saved

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
                # 断言在 patch 块内：source_registry() 的跨进程 mtime 检测
                # 只针对**本 registry 构建时**的存储指纹；块外调用会读到
                # 真实部署存储（生产源与本测试无关）。
                ids = {
                    provider.config.source_id
                    for provider in sp.source_registry()._providers.values()
                }
            self.assertIn("static-local", ids)
            self.assertIn("user-a17", ids)

    def test_registry_auto_reload_detects_cross_process_changes(self):
        """mtime+size 变化检测：另一进程写入 store 后读路径自动重建。"""
        import features.system.source_provider as sp

        with TemporaryDirectory() as directory:
            store_path = Path(directory) / "user_sources.json"
            store = UserSourceStore(store_path)
            store_path.write_text('{"version": 1, "sources": {}}', encoding="utf-8")
            _stub_secrets(store)
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
                self.assertEqual(sp.initialize_source_runtime(static_config), "AVAILABLE")
                self.assertIn(
                    "static-local",
                    {
                        p.config.source_id
                        for p in sp.source_registry()._providers.values()
                    },
                )
                # 模拟另一进程（Web worker B）添加 SDK 源：直接改文件。
                store.add(
                    source_id="user-b",
                    repo_root="/sdk/b",
                    ssh_host="10.0.0.9",
                    ssh_port=22,
                    ssh_username="builder",
                    ssh_password="pw",
                )
                ids = {
                    p.config.source_id
                    for p in sp.source_registry()._providers.values()
                }
                self.assertIn("user-b", ids)  # 读路径自动重建
                self.assertIn("static-local", ids)

    def test_registry_rebuilds_when_store_file_disappears(self):
        """存储文件被删除：读路径必须重建（用户源消失），不能沿用旧 registry。

        2026-11-14 审核意见：旧指纹把"文件不存在"与"无变化"混为同一
        返回值，文件消失后旧 registry（含已删除文件里的用户源）被静默
        沿用。现在"确认不存在"是独立指纹态，与构建时基线不同 → stale。
        """
        import features.system.source_provider as sp

        with TemporaryDirectory() as directory:
            store_path = Path(directory) / "user_sources.json"
            store = UserSourceStore(store_path)
            _stub_secrets(store)
            store.add(
                source_id="user-gone",
                repo_root="/sdk/gone",
                ssh_host="10.0.0.9",
                ssh_port=22,
                ssh_username="builder",
                ssh_password="pw",
            )
            static_config = {"sdk_sources": {"providers": [
                {"source_id": "static-local", "provider": "local_git",
                 "repo_root": "/local/repo"},
            ]}}
            with mock.patch.object(
                sp, "user_source_store", return_value=store
            ), mock.patch.object(
                sp, "derive_application_key", create=True
            ) as derive:
                derive.return_value = b"k"
                self.assertEqual(
                    sp.initialize_source_runtime(static_config), "AVAILABLE")
                self.assertIn(
                    "user-gone",
                    {p.config.source_id
                     for p in sp.source_registry()._providers.values()},
                )
                # 模拟管理员手工删除存储文件。
                store_path.unlink()
                ids = {
                    p.config.source_id
                    for p in sp.source_registry()._providers.values()
                }
                self.assertNotIn("user-gone", ids)  # 读路径重建：用户源消失
                self.assertIn("static-local", ids)  # 静态源不受影响

    def test_corrupt_store_breaks_hot_reload_without_reset(self):
        """热更新读到损坏存储：保留旧 registry，不静默清空。"""
        import features.system.source_provider as sp

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
            with mock.patch.object(
                sp, "user_source_store", return_value=store
            ), mock.patch.object(
                sp, "derive_application_key", create=True
            ) as derive:
                derive.return_value = b"k"
                self.assertEqual(sp.initialize_source_runtime({}), "AVAILABLE")
                # 另一进程写入后文件损坏。
                store_path.write_text("{broken", encoding="utf-8")
                registry = sp.source_registry()  # 重建失败 → 旧 registry
                ids = {
                    p.config.source_id for p in registry._providers.values()
                }
                self.assertIn("user-a16", ids)

    def test_sdk_availability_refreshes_after_another_process_adds_or_deletes(self):
        sp = self._sp
        with TemporaryDirectory() as directory:
            store = UserSourceStore(Path(directory) / "sources.json")
            with mock.patch.object(sp, "user_source_store", return_value=store), \
                 mock.patch("foundation.secrets.derive_application_key", return_value=b"test"):
                sp.initialize_source_runtime({})
                self.assertFalse(sp.sdk_sources_available())
                store.add(source_id="later", provider="local_git", repo_root="/sdk/later")
                self.assertTrue(sp.sdk_sources_available())
                store.delete("later")
                self.assertFalse(sp.sdk_sources_available())

    def test_write_during_registry_build_is_detected_on_next_read(self):
        sp = self._sp
        with TemporaryDirectory() as directory:
            store = UserSourceStore(Path(directory) / "sources.json")
            build = sp._build_registry_configs

            def build_then_write(config):
                configs = build(config)
                store.add(source_id="concurrent", provider="local_git", repo_root="/sdk/new")
                return configs

            with mock.patch.object(sp, "user_source_store", return_value=store), \
                 mock.patch("foundation.secrets.derive_application_key", return_value=b"test"):
                with mock.patch.object(sp, "_build_registry_configs", side_effect=build_then_write):
                    sp.initialize_source_runtime({})
                ids = {source["source_id"] for source in sp.source_registry().list_sources()}
                self.assertIn("concurrent", ids)


if __name__ == "__main__":
    unittest.main()
