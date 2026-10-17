"""工作量统计的 JSON 快照列按需 decode 回归。

审核项：get_workload_statistics 曾对全表行 decode journals_json/
attachments_json/failures_json，而已解决且带 closed_on 的行（表中的
大头）从不读这些列。现在轻列扫描 + 仅对参与计算的行批量补拉。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine.repository import RedmineAgentDB


def _issue(issue_id, assigned_to_name, status_name="新建", closed_on="", journals=None, attachments=None):
    return {
        "issue_id": issue_id,
        "subject": f"Issue {issue_id}",
        "status_name": status_name,
        "priority_name": "正常",
        "assigned_to_name": assigned_to_name,
        "created_on": "2026-05-01T00:00:00",
        "updated_on": "2026-06-10T00:00:00",
        "closed_on": closed_on,
        "journals_json": journals or [],
        "attachments_json": attachments or [],
        "failures_json": [],
        "is_resolved": 1 if status_name in ("已解决", "已关闭") else 0,
        "last_scanned_at": "2026-06-10T00:00:00",
    }


class WorkloadLazyJsonTests(unittest.TestCase):
    def test_json_columns_decoded_only_for_issues_that_use_them(self):
        with TemporaryDirectory() as tmp:
            db = RedmineAgentDB(db_path=Path(tmp) / "redmine.sqlite3", docs_dir=Path(tmp) / "docs")
            heavy = [{"user": "客户", "created_on": "2026-06-01T00:00:00", "notes": "note " * 2000}]
            # 已解决且带 closed_on：从不读 JSON 列。
            db.upsert_issue(_issue(101, "张三", status_name="已关闭", closed_on="2026-06-01T00:00:00", journals=heavy))
            # 已解决但缺 closed_on：journals 用于回溯关闭时间。
            db.upsert_issue(_issue(102, "张三", status_name="已关闭", closed_on="", journals=[{
                "user": "张三", "created_on": "2026-06-05T00:00:00", "notes": "",
                "details": [{"name": "status", "new_value": "已关闭"}],
            }]))
            # 未解决：journals 参与等待归因。
            db.upsert_issue(_issue(103, "张三", journals=[{
                "user": "客户", "created_on": "2026-06-12T00:00:00", "notes": "请处理",
            }]))

            real_loads = json.loads
            calls = []

            def counting_loads(value):
                calls.append(value)
                return real_loads(value)

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime, \
                    patch.object(json, "loads", side_effect=counting_loads):
                mocked_datetime.now.return_value = datetime(2026, 6, 13, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = db.get_workload_statistics(owner_names=["张三"], stale_days=3, list_limit=10)

            # 返回结构不变：计数与趋势桶和全量 decode 时代一致。
            self.assertEqual(stats["total_owned"], 3)
            self.assertEqual(stats["closed_count"], 2)
            self.assertEqual(stats["open_count"], 1)
            self.assertEqual(stats["waiting_my_reply"], 1)
            daily = {row["date"]: row["count"] for row in stats["resolved_daily"]}
            self.assertEqual(daily.get("2026-06-01"), 1)
            self.assertEqual(daily.get("2026-06-05"), 1)

            # 只有 102/103 需要补拉 JSON（各 3 列）；101 的重 journals 不 decode。
            self.assertEqual(len(calls), 2 * 3)

    def test_lazy_decode_keeps_missing_report_and_attachment_count(self):
        with TemporaryDirectory() as tmp:
            db = RedmineAgentDB(db_path=Path(tmp) / "redmine.sqlite3", docs_dir=Path(tmp) / "docs")
            # 未解决工单的 attachments_json 参与“缺测试报告”判定与 attachment_count。
            db.upsert_issue(_issue(201, "张三", journals=[{
                "user": "客户", "created_on": "2026-06-12T00:00:00", "notes": "请处理",
            }], attachments=[{"filename": "test_result.xml"}]))
            db.upsert_issue(_issue(202, "张三", journals=[{
                "user": "客户", "created_on": "2026-06-12T00:00:00", "notes": "请处理",
            }]))
            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 13, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = db.get_workload_statistics(owner_names=["张三"], stale_days=3, list_limit=10)

            self.assertEqual(stats["missing_test_report"], 1)
            open_items = {item["issue_id"]: item for item in stats["lists"]["open_issues"]}
            self.assertEqual(open_items[201]["attachment_count"], 1)
            self.assertEqual(open_items[202]["attachment_count"], 0)


if __name__ == "__main__":
    unittest.main()
