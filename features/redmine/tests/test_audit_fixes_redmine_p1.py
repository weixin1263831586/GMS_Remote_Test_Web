"""2026-10-09 全库审计 P1 修复回归（org_chart / case_extractor / reply_drafter）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine import org_chart
from features.redmine import users as redmine_users
from features.redmine.case_extractor import RedmineCaseExtractor
from features.redmine.knowledge_repository import RedmineKnowledgeDB
from features.redmine.reply_drafter import (
    DEFAULT_OWNER_REPLY_NAMES,
    ReplyDrafter,
    load_owner_reply_names,
)


ROSTER = {"departments": [{
    "department_id": "sys2", "department": "系统二部",
    "members": [{"id": 8912, "name": "张三"}, {"id": 8913, "name": "李四"}],
}]}


class OrgChartAuditFixTests(unittest.TestCase):
    """坏 JSON / 空结构不得清空组织花名册。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fake_settings = type("S", (), {"project_root": self.root, "data_root": self.root})()
        for target in (org_chart, redmine_users):
            p = patch.object(target, "settings", fake_settings)
            p.start()
            self.addCleanup(p.stop)

    def _write_roster(self) -> Path:
        org_chart.save_org_payload(json.loads(json.dumps(ROSTER)), self.root)
        return org_chart.org_chart_path(self.root)

    def test_corrupt_json_does_not_wipe_roster(self):
        path = self._write_roster()
        corrupt = "{broken"
        path.write_text(corrupt, encoding="utf-8")
        # 读取显式失败，upsert 不得把“空结构 + 新成员”整体覆写回磁盘。
        with self.assertRaises(ValueError):
            org_chart.load_org_payload(self.root)
        with self.assertRaises(ValueError):
            org_chart.upsert_org_member(
                {"id": 1, "name": "王五"},
                {"department_id": "sys2", "department": "系统二部"},
                project_root=self.root,
            )
        self.assertEqual(path.read_text(encoding="utf-8"), corrupt)

    def test_save_refuses_empty_departments_over_nonempty_roster(self):
        path = self._write_roster()
        with self.assertRaises(ValueError):
            org_chart.save_org_payload({"departments": []}, self.root)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["departments"][0]["members"]), 2)

    def test_save_rejects_non_dict_payload(self):
        path = self._write_roster()
        with self.assertRaises(ValueError):
            org_chart.save_org_payload(["not", "a", "dict"], self.root)  # type: ignore[arg-type]
        json.loads(path.read_text(encoding="utf-8"))

    def test_save_allows_empty_and_repair_paths(self):
        # 首次落盘（无现有花名册）允许空 departments。
        org_chart.save_org_payload({"departments": []}, self.root)
        # 现有文件损坏时允许用完整合法 payload 覆写修复。
        path = org_chart.org_chart_path(self.root)
        path.write_text("{broken", encoding="utf-8")
        org_chart.save_org_payload(ROSTER, self.root)
        self.assertEqual(
            org_chart.load_org_payload(self.root)["departments"][0]["members"],
            ROSTER["departments"][0]["members"])

    def test_save_leaves_no_tmp_file(self):
        path = self._write_roster()
        self.assertFalse(path.with_name(path.name + ".tmp").exists())


class CaseExtractorAuditFixTests(unittest.TestCase):
    """提取器只提取不编造：签名命中不得触发罐头 verification/根因/方案。"""

    def test_signature_hits_never_get_canned_conclusions(self):
        issues = [
            {"issue_id": 1, "subject": "vbmeta signed with VBMeta test key",
             "description": "", "status_name": "New"},
            {"issue_id": 2, "subject": "VtsHalPowerTargetTest hasFixedPerformance fail",
             "description": "Power/PowerAidl#hasFixedPerformance Actual: false", "status_name": "New"},
        ]
        for issue in issues:
            fact = RedmineCaseExtractor.extract(issue)
            self.assertTrue(fact["error_signature"])
            self.assertEqual(fact["verification"], "")
            self.assertEqual(fact["root_cause"], "")
            self.assertEqual(fact["solution"], "")
            self.assertNotIn("验证方式", fact["reply_template"])


class ReplyDrafterAuditFixTests(unittest.TestCase):
    """owner 回复锚点名单可配置，默认行为保持。"""

    def setUp(self):
        self.db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))

    def test_default_names_still_extract_hcq_block(self):
        drafter = ReplyDrafter(self.db, owner_reply_names=DEFAULT_OWNER_REPLY_NAMES)
        excerpt = "### 黄超群\n用 production key 重签名\n### 客户\n收到"
        self.assertIn("production key", drafter._extract_owner_reply(excerpt))

    def test_injected_names_replace_default_anchor(self):
        drafter = ReplyDrafter(self.db, owner_reply_names=["李 四"])
        excerpt = "### 李四(FAE)\n步骤A\n### 黄超群\n旧专家回复"
        self.assertIn("步骤A", drafter._extract_owner_reply(excerpt))
        # 名单未包含的姓名不再作为锚点。
        self.assertNotIn("旧专家回复", drafter._extract_owner_reply(excerpt))

    def test_empty_names_extract_nothing(self):
        drafter = ReplyDrafter(self.db, owner_reply_names=[])
        self.assertEqual(drafter._extract_owner_reply("### 黄超群\n任何内容"), "")

    def test_load_owner_reply_names_reads_redmine_agent_config(self):
        stub = type("M", (), {"load_config": lambda self: {
            "redmine_agent": {"owner_reply_names": ["张三", " 王五 "]}}})()
        with patch("features.redmine.config.config_manager", stub):
            self.assertEqual(load_owner_reply_names(), ("张三", "王五"))

    def test_load_owner_reply_names_falls_back_to_default(self):
        cases = [
            {"redmine_agent": {"owner_reply_names": []}},
            {"redmine_agent": {"owner_reply_names": "张三"}},  # 单字符串也接受
            {"redmine_agent": {}},
            {},
        ]
        stub = type("M", (), {"load_config": lambda self, c=None: {}})()
        with patch("features.redmine.config.config_manager", stub):
            self.assertEqual(load_owner_reply_names(), DEFAULT_OWNER_REPLY_NAMES)
        for cfg in cases:
            stub = type("M", (), {"load_config": lambda self, c=cfg: c})()
            with patch("features.redmine.config.config_manager", stub):
                names = load_owner_reply_names()
                if cfg.get("redmine_agent", {}).get("owner_reply_names") == "张三":
                    self.assertEqual(names, ("张三",))
                else:
                    self.assertEqual(names, DEFAULT_OWNER_REPLY_NAMES)


if __name__ == "__main__":
    unittest.main()
