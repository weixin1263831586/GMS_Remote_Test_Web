from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.knowledge.service import KnowledgeService
from features.knowledge.storage import KnowledgeStore


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = KnowledgeStore(db_path=root / "knowledge.sqlite3", attachment_dir=root / "attachments")
        self.service = KnowledgeService(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_spaces_and_tree_doc_flow(self):
        spaces = self.store.list_spaces("u1")
        self.assertGreaterEqual(len(spaces), 3)

        folder = self.store.create_folder("u1", "gms", "CTS问题")
        doc = self.service.create_doc_from_text(
            "u1",
            space_id="gms",
            parent_id=folder["node_id"],
            title="CTS Fail",
            content="RK3588 CTS Camera fail root cause",
            tags=["CTS", "RK3588"],
            links=[{"target_type": "redmine_issue", "target_id": "123", "title": "#123"}],
        )

        self.assertEqual(doc["title"], "CTS Fail")
        self.assertEqual(doc["tags"], ["CTS", "RK3588"])
        self.assertEqual(doc["links"][0]["target_type"], "redmine_issue")
        tree = self.store.list_tree("u1", "gms")
        self.assertTrue(any(n["node_id"] == folder["node_id"] for n in tree))
        self.assertTrue(any(n.get("doc_id") == doc["doc_id"] for n in tree))

    def test_default_spaces_are_stable_for_multiple_users(self):
        first = self.store.list_spaces("u1")
        second = self.store.list_spaces("u2")

        self.assertEqual(first[0]["space_id"], "gms")
        self.assertNotEqual(second[0]["space_id"], "gms")
        self.assertEqual(self.store.default_space_id("u1"), first[0]["space_id"])
        self.assertEqual(self.store.default_space_id("u2"), second[0]["space_id"])
        for base_id in ("gms", "devices", "issues"):
            resolved = self.store.default_space_id("u2", base_id)
            self.assertIn(resolved, {item["space_id"] for item in second})
            self.assertIn(base_id, resolved)

    def test_api_space_alias_resolves_to_each_users_owned_default(self):
        from features.knowledge.api import _space_id

        with patch("features.knowledge.api._service", self.service):
            first = _space_id("u1", "issues")
            second = _space_id("u2", "issues")

        self.assertEqual(first, "issues")
        self.assertNotEqual(second, "issues")
        self.assertIn(second, {item["space_id"] for item in self.store.list_spaces("u2")})

    def test_search_indexes_content_tags_and_links(self):
        doc = self.service.create_doc_from_text(
            "u1",
            space_id="issues",
            title="Gerrit fix",
            content="Fix VTS module timeout",
            tags="VTS,Timeout",
            links=[{"target_type": "gerrit_change", "target_id": "456"}],
        )

        self.assertEqual(self.store.search("u1", "VTS")[0]["doc_id"], doc["doc_id"])
        self.assertEqual(self.store.search("u1", "456")[0]["doc_id"], doc["doc_id"])

    def test_update_and_delete_doc(self):
        doc = self.service.create_doc_from_text("u1", space_id="gms", title="Old", content="old body")

        updated = self.store.update_doc("u1", doc["doc_id"], {"title": "New", "content_md": "new body", "tags": "A,B"})
        self.assertEqual(updated["title"], "New")
        self.assertEqual(updated["tags"], ["A", "B"])
        self.assertTrue(self.store.delete_node("u1", updated["node_id"]))
        self.assertIsNone(self.store.get_doc("u1", doc["doc_id"]))

    def test_version_history_and_restore_are_user_isolated(self):
        doc = self.service.create_doc_from_text(
            "u1", space_id="gms", title="Version 1", content="first", tags=["old"]
        )
        self.store.update_doc(
            "u1", doc["doc_id"], {"title": "Version 2", "content_md": "second", "tags": ["new"]}
        )

        versions = self.store.list_versions("u1", doc["doc_id"])
        self.assertEqual([item["version_no"] for item in versions], [2, 1])
        restored = self.store.restore_version("u1", doc["doc_id"], versions[-1]["version_id"])
        self.assertEqual(restored["title"], "Version 1")
        self.assertEqual(restored["content_md"], "first")
        self.assertEqual(restored["tags"], ["old"])
        self.assertEqual(self.store.list_versions("u2", doc["doc_id"]), [])
        self.assertIsNone(self.store.restore_version("u2", doc["doc_id"], versions[-1]["version_id"]))

    def test_delete_folder_removes_all_nested_descendants(self):
        root = self.store.create_folder("u1", "gms", "Root")
        child = self.store.create_folder(
            "u1", "gms", "Child", parent_id=root["node_id"]
        )
        doc = self.service.create_doc_from_text(
            "u1",
            space_id="gms",
            parent_id=child["node_id"],
            title="Nested",
            content="nested content",
        )

        self.assertTrue(self.store.delete_node("u1", root["node_id"]))

        self.assertIsNone(self.store.get_doc("u1", doc["doc_id"]))
        self.assertFalse(
            any(
                item["node_id"] in {root["node_id"], child["node_id"]}
                for item in self.store.list_tree("u1", "gms")
            )
        )

    def test_ask_retrieves_gts_guide_context_for_ai(self):
        self.service.create_doc_from_text(
            "u1",
            space_id="gms",
            title="Android_GMS_Developer_Guide_CN",
            content=(
                "# Android GMS Developer Guide\n\n"
                "GTS 测试用于验证 Google Mobile Services 集成。"
                "执行前需要准备 gts-tradefed、测试账号、网络环境和目标设备。"
                "常用入口是进入 android-gts/tools 后运行 ./gts-tradefed。"
            ),
            tags=["GTS", "GMS"],
        )

        class Analyzer:
            def generate(self, user_prompt, system_prompt="", max_tokens=None, preferred_provider=None):
                self.prompt = user_prompt
                self.preferred_provider = preferred_provider
                return {"success": True, "content": "GTS 测试需要使用 gts-tradefed，并准备账号、网络和设备。", "provider": "fake"}

        analyzer = Analyzer()
        with patch("features.knowledge.service.get_universal_analyzer", return_value=analyzer):
            result = self.service.ask("u1", "GTS测试", space_id="gms")

        self.assertEqual(result["mode"], "ai")
        self.assertEqual(analyzer.preferred_provider, "glm_local")
        self.assertIn("gts-tradefed", analyzer.prompt)
        self.assertIn("GTS 测试", result["contexts"][0]["snippet"])
        self.assertEqual(result["contexts"][0]["title"], "Android_GMS_Developer_Guide_CN")


if __name__ == "__main__":
    unittest.main()
