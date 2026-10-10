"""Regression tests for the 2026-10-09 full-code-audit P1 fixes.

覆盖：硬编码附件清单移除（含 per-owner base_url 传递）、_start 的
check-then-act 竞态、assignee 快照批量抓取的单条容错、主库连接的
WAL/busy_timeout 规约。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class _FakeRepository:
    def __init__(self):
        self.stale_mark_count = 0

    def mark_stale_running_runs(self):
        self.stale_mark_count += 1


class _FakeAgent:
    def _make_client(self):
        raise AssertionError("not used by these tests")


class AttachmentLinkTests(unittest.TestCase):
    def test_attachment_links_use_owner_base_url_and_never_fabricate(self):
        from features.redmine.api import _attachment_links_for_issue

        issue = {
            "issue_id": 123,
            "attachments_json": [{"attachment_id": "9", "filename": "log.zip"}],
        }
        links = _attachment_links_for_issue(issue, base_url="https://owner.example")
        self.assertEqual(
            [item["url"] for item in links],
            ["https://owner.example/attachments/download/9/"],
        )

        # 缺附件元数据的工单（含曾硬编码的 598972）不得伪造附件清单。
        empty = _attachment_links_for_issue(
            {"issue_id": 598972, "attachments_json": []},
            base_url="https://owner.example",
        )
        self.assertEqual(empty, [])


class StartRunRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_start_never_reports_ghost_run_id(self):
        from features.redmine.service import RedmineService

        service = RedmineService(repository=_FakeRepository(), agent=_FakeAgent())
        ran: list[str] = []
        release = asyncio.Event()

        async def operation_a():
            ran.append("a")
            await release.wait()

        async def operation_b():
            ran.append("b")

        # 制造 TOCTOU 窗口：占住 SingleFlightTask 内部锁，让两个 _start 都
        # 通过运行中预检查后在 task.start 处挂起。
        async with service.task._lock:
            first = asyncio.create_task(
                service._start(run_id="run-a", message="a", operation=operation_a)
            )
            await asyncio.sleep(0)
            second = asyncio.create_task(
                service._start(run_id="run-b", message="b", operation=operation_b)
            )
            await asyncio.sleep(0)

        results = await asyncio.gather(first, second)
        release.set()
        await service.task.cancel()

        winners = [r for r in results if r.get("success")]
        losers = [r for r in results if not r.get("success")]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["run_id"], "run-a")
        # 落败方必须显式报冲突并指向真正在跑的 run，而不是返回自己从未
        # 启动过的幽灵 run_id。
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0]["run_id"], "run-a")
        self.assertNotIn("b", ran)
        self.assertEqual(service.status()["active_run_id"], "run-a")


class AssigneeSnapshotBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_batch_survives_single_issue_failure(self):
        from features.redmine.client import RedmineClient

        client = RedmineClient("https://redmine.example")

        async def fake_fetch(**kwargs):
            return [
                SimpleNamespace(id=1, updated_on="2026-01-01T00:00:00Z"),
                SimpleNamespace(id=2, updated_on="2026-01-01T00:00:00Z"),
            ]

        client.fetch_issues_by_assignee = fake_fetch

        class _Issues:
            def get(self, issue_id, include=None):
                if int(issue_id) == 2:
                    raise PermissionError("403 Forbidden")
                return SimpleNamespace(
                    id=1,
                    subject="ok",
                    status=None,
                    priority=None,
                    assigned_to=None,
                    created_on="",
                    updated_on="",
                    closed_on="",
                    description="",
                    journals=[],
                )

        client._redmine = SimpleNamespace(issue=_Issues())

        snapshots = await client.fetch_open_issue_snapshots_by_assignee(assignee_id=7)

        self.assertEqual([item["issue_id"] for item in snapshots], [1])


class RepositoryConnectionPragmasTest(unittest.TestCase):
    def test_connect_enables_wal_and_busy_timeout(self):
        from features.redmine.repository import RedmineAgentDB

        with tempfile.TemporaryDirectory() as root:
            repo = RedmineAgentDB(Path(root) / "redmine.sqlite3", Path(root) / "docs")
            conn = repo.connect()
            try:
                mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
            finally:
                conn.close()
            self.assertEqual(mode, "wal")
            self.assertEqual(busy_timeout, 30000)


if __name__ == "__main__":
    unittest.main()
