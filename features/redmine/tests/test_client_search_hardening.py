"""client.search_issues_by_subject 的 None-safe 归一化回归。

审核项：结果字典曾用 ``getattr(issue, "status", None).name`` 链式取值，
issue 缺 status/project 属性时直接 AttributeError。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from features.redmine import client as client_module


class SubjectSearchNoneSafeTests(unittest.TestCase):
    def test_issues_without_status_or_project_attrs_do_not_crash(self):
        redmine_client = object.__new__(client_module.RedmineClient)

        class _Filter:
            def __call__(self, **kwargs):
                return [
                    SimpleNamespace(id=1, subject="s1", updated_on="2026-10-01T00:00:00Z"),
                    SimpleNamespace(
                        id=2,
                        subject="s2",
                        updated_on="2026-10-02T00:00:00Z",
                        status=SimpleNamespace(name="反馈"),
                        project=SimpleNamespace(name="fae"),
                    ),
                ]

        resource = SimpleNamespace(issue=SimpleNamespace(filter=_Filter()))
        redmine_client._redmine = resource

        rows = asyncio.run(redmine_client.search_issues_by_subject("term"))
        self.assertEqual(rows[0]["status_name"], "")
        self.assertEqual(rows[0]["project_name"], "")
        self.assertEqual(rows[1]["status_name"], "反馈")
        self.assertEqual(rows[1]["project_name"], "fae")


if __name__ == "__main__":
    unittest.main()
