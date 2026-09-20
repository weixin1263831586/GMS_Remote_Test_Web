"""org_chart（方案 2：全局组织架构 + per-owner overlay）单元测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine import org_chart
from features.redmine.org_chart import (
    effective_user_map,
    get_self_binding,
    load_org_payload,
    load_user_overlay,
    set_member_aliases,
    set_self_binding,
    upsert_org_member,
)


class OrgChartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fake_settings = type("S", (), {"project_root": self.root, "data_root": self.root})()
        # org_chart 与 users（owner_user_map_path）两侧的 settings 都要指向
        # 临时目录，否则会读写真实 data/ 下的 owner 文件。
        self.settings_patch = patch.object(org_chart, "settings", fake_settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        from features.redmine import users as redmine_users

        self.users_settings_patch = patch.object(
            redmine_users, "settings", fake_settings)
        self.users_settings_patch.start()
        self.addCleanup(self.users_settings_patch.stop)

    def _write_org(self, payload: dict) -> None:
        org_chart.save_org_payload(payload, self.root)

    def test_missing_files_yield_empty_map(self):
        self.assertEqual(effective_user_map("alice"), [])
        self.assertIsNone(get_self_binding("alice"))
        self.assertEqual(load_user_overlay("alice"), {})

    def test_effective_map_merges_overlay_aliases(self):
        self._write_org({"departments": [{
            "department_id": "sys2", "department": "系统二部",
            "members": [{"id": 8912, "name": "黄超群", "email": "cq@ex.com"}],
        }]})
        set_member_aliases("alice", 8912, ["超群", " CQ "])
        members = effective_user_map("alice")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["aliases"], ["超群", "CQ"])
        # 别名是 per-owner 的：bob 看不到。
        self.assertNotIn("aliases", effective_user_map("bob")[0])

    def test_self_binding_requires_existing_member(self):
        self._write_org({"departments": [{
            "department_id": "d", "department": "部",
            "members": [{"id": 7, "name": "张三"}],
        }]})
        member = set_self_binding("alice", 7)
        self.assertEqual(member["name"], "张三")
        self.assertEqual(get_self_binding("alice")["id"], 7)
        # 绑定不存在成员 → KeyError 且不落盘。
        with self.assertRaises(KeyError):
            set_self_binding("alice", 99)
        self.assertEqual(get_self_binding("alice")["id"], 7)
        # 解绑。
        set_self_binding("alice", None)
        self.assertIsNone(get_self_binding("alice"))

    def test_overlay_rejects_departments_key(self):
        # 旧格式文件（含 departments）被忽略；写侧也剥离组织架构键。
        owner_file = org_chart.owner_user_map_path("alice")
        owner_file.parent.mkdir(parents=True, exist_ok=True)
        owner_file.write_text(json.dumps({
            "departments": [{"department_id": "stale", "members": [{"id": 1}]}],
            "me": 7,
        }), encoding="utf-8")
        self.assertEqual(load_org_payload(self.root)["departments"], [])
        self.assertEqual(load_user_overlay("alice"), {"me": 7})
        org_chart.save_user_overlay("alice", {"me": 7, "departments": [{"x": 1}]})
        self.assertNotIn("departments", json.loads(owner_file.read_text()))

    def test_upsert_member_create_and_move(self):
        result = upsert_org_member(
            {"id": 5, "name": "王五", "email": "w@ex.com"},
            {"department_id": "sys2", "department": "系统二部"},
            project_root=self.root,
        )
        self.assertTrue(result["created"])
        payload = load_org_payload(self.root)
        self.assertEqual(payload["departments"][0]["department"], "系统二部")
        # 同 id 换部门：从旧部门移除，只留新部门一份。
        result = upsert_org_member(
            {"id": 5, "name": "王五"}, {"department_id": "sys1", "department": "系统一部"},
            project_root=self.root,
        )
        self.assertFalse(result["created"])
        flat = {m["id"]: m.get("department_id") for m in effective_user_map("alice")}
        self.assertEqual(list(flat.values()), ["sys1"])

    def test_corrupt_org_file_falls_back_to_empty(self):
        path = org_chart.org_chart_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(load_org_payload(self.root), {"departments": []})
        self.assertEqual(effective_user_map("alice"), [])


if __name__ == "__main__":
    unittest.main()
