"""Evidence 快照保留策略（磁盘回收）回归。

证据快照附件原样落盘，实测单工单可达 GB 级且历史上无任何清理，是
data/ 下最大的持续磁盘开销。本文件锁定三条契约：
- 终态快照按年龄（GMS_EVIDENCE_RETENTION_DAYS）与每工单数量
  （GMS_EVIDENCE_MAX_SNAPSHOTS_PER_ISSUE）回收，进行中状态永不回收；
- 文件先删、DB 行后删，DB 行只随文件一起消失（不产生悬挂引用）；
- 无 DB 引用的孤儿目录（先删后崩的残留）由清扫兜底回收。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from features.redmine import evidence_store
from features.redmine.evidence_store import EvidenceStore


class EvidenceRetentionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = EvidenceStore(Path(self._tmp.name))
        # 隔离限频状态：create_snapshot 内嵌的限频触发在测试里跳过，
        # 由用例直接调 apply_retention 断言。
        reset = mock.patch.object(evidence_store, "_last_retention_sweep", time.monotonic())
        reset.start()
        self.addCleanup(reset.stop)

    def _backdate(self, snapshot_id: str, days: float) -> None:
        created = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self.store._lock, self.store._connect() as conn:
            conn.execute(
                "UPDATE redmine_evidence_snapshots SET created_at = ? "
                "WHERE snapshot_id = ?",
                (created, snapshot_id),
            )

    def _snapshot_dir(self, issue_id: int, snapshot_id: str) -> Path:
        directory = self.store.evidence_dir / str(issue_id) / snapshot_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "issue.json").write_text("{}", encoding="utf-8")
        return directory

    def test_beyond_per_issue_limit_recycles_oldest(self):
        with mock.patch.dict(
            os.environ, {"GMS_EVIDENCE_MAX_SNAPSHOTS_PER_ISSUE": "2"}
        ):
            old = self.store.create_snapshot(issue_id=1)
            middle = self.store.create_snapshot(issue_id=1)
            newest = self.store.create_snapshot(issue_id=1)
            for snapshot in (old, middle, newest):
                self.store.update_snapshot(snapshot["snapshot_id"], status="ready")
            old_dir = self._snapshot_dir(1, old["snapshot_id"])
            middle_dir = self._snapshot_dir(1, middle["snapshot_id"])
            newest_dir = self._snapshot_dir(1, newest["snapshot_id"])

            stats = self.store.apply_retention()

        self.assertEqual(stats["deleted_snapshots"], 1)
        self.assertFalse(old_dir.exists())
        self.assertTrue(middle_dir.exists())
        self.assertTrue(newest_dir.exists())
        self.assertIsNone(self.store.get_snapshot(old["snapshot_id"]))
        self.assertIsNotNone(self.store.get_snapshot(newest["snapshot_id"]))

    def test_age_cutoff_recycles_old_ready_snapshots(self):
        with mock.patch.dict(os.environ, {"GMS_EVIDENCE_RETENTION_DAYS": "1"}):
            victim = self.store.create_snapshot(issue_id=7)
            keep = self.store.create_snapshot(issue_id=7)
            for snapshot in (victim, keep):
                self.store.update_snapshot(snapshot["snapshot_id"], status="ready")
            self._backdate(victim["snapshot_id"], days=2)
            victim_artifact = self.store.create_artifact(
                snapshot_id=victim["snapshot_id"],
                attachment_id="42",
                filename="uart.log",
                original_filename="uart.log",
                content_type="text/plain",
                kind="log",
            )
            victim_dir = self._snapshot_dir(7, victim["snapshot_id"])

            stats = self.store.apply_retention()

        self.assertEqual(stats["deleted_snapshots"], 1)
        self.assertFalse(victim_dir.exists())
        self.assertIsNone(self.store.get_snapshot(victim["snapshot_id"]))
        self.assertIsNone(self.store.get_artifact(victim_artifact["artifact_id"]))
        self.assertIsNotNone(self.store.get_snapshot(keep["snapshot_id"]))

    def test_active_snapshots_are_never_recycled(self):
        with mock.patch.dict(os.environ, {"GMS_EVIDENCE_RETENTION_DAYS": "1"}):
            fetching = self.store.create_snapshot(issue_id=3)
            queued = self.store.create_snapshot(issue_id=4)
            self.store.update_snapshot(fetching["snapshot_id"], status="fetching")
            self.store.update_snapshot(queued["snapshot_id"], status="queued")
            for snapshot in (fetching, queued):
                self._backdate(snapshot["snapshot_id"], days=30)

            stats = self.store.apply_retention()

        self.assertEqual(stats["deleted_snapshots"], 0)
        self.assertIsNotNone(self.store.get_snapshot(fetching["snapshot_id"]))
        self.assertIsNotNone(self.store.get_snapshot(queued["snapshot_id"]))

    def test_orphan_snapshot_dirs_are_swept(self):
        orphans = self._snapshot_dir(9, "ev_orphan_no_db_row")
        old = time.time() - 3 * 24 * 60 * 60
        os.utime(orphans, (old, old))
        referenced = self.store.create_snapshot(issue_id=9)
        self.store.update_snapshot(referenced["snapshot_id"], status="ready")
        referenced_dir = self._snapshot_dir(9, referenced["snapshot_id"])
        os.utime(referenced_dir, (old, old))
        fresh_orphan = self._snapshot_dir(9, "ev_orphan_fresh")

        stats = self.store.apply_retention()

        self.assertFalse(orphans.exists())
        self.assertTrue(referenced_dir.exists())
        self.assertTrue(fresh_orphan.exists())
        self.assertGreaterEqual(stats["removed_dirs"], 1)

    def test_maybe_apply_retention_rate_limits(self):
        with mock.patch.dict(os.environ, {"GMS_EVIDENCE_RETENTION_DAYS": "1"}):
            victim = self.store.create_snapshot(issue_id=11)
            self.store.update_snapshot(victim["snapshot_id"], status="ready")
            self._backdate(victim["snapshot_id"], days=5)

            first = evidence_store.maybe_apply_retention(self.store, min_interval=0)
            self.assertIsNotNone(first)
            self.assertEqual(first["deleted_snapshots"], 1)

            again = self.store.create_snapshot(issue_id=11)
            self.store.update_snapshot(again["snapshot_id"], status="ready")
            self._backdate(again["snapshot_id"], days=5)

            # 限频窗口内顺带触发不执行（返回 None，快照保留）。
            skipped = evidence_store.maybe_apply_retention(self.store)
            self.assertIsNone(skipped)
            self.assertIsNotNone(self.store.get_snapshot(again["snapshot_id"]))


if __name__ == "__main__":
    unittest.main()
