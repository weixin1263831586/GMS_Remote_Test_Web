"""Evidence store retention, attachment lookup and snapshot-state contracts."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from features.redmine.evidence import store as evidence_store
from features.redmine.evidence.service import snapshot_completeness
from features.redmine.evidence.store import EvidenceStore


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


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_store, "_now", lambda: "2026-10-27T10:00:00")
    return evidence_store.EvidenceStore(tmp_path)


def _artifact(store, snapshot, monkeypatch, artifact_id):
    monkeypatch.setattr(evidence_store, "new_artifact_id", lambda: artifact_id)
    return store.create_artifact(
        snapshot_id=snapshot["snapshot_id"], attachment_id="123",
        filename="log.txt", original_filename="log.txt",
        content_type="text/plain", kind="text",
    )


def test_default_lookup_uses_newest_snapshot_when_older_download_finishes_late(store, monkeypatch):
    old = store.create_snapshot(issue_id=1)
    new = store.create_snapshot(issue_id=1)
    newest = _artifact(store, new, monkeypatch, "art_a")
    older = _artifact(store, old, monkeypatch, "art_z")

    assert store.latest_snapshot_for_issue(1)["snapshot_id"] == new["snapshot_id"]
    assert store.get_artifact_by_attachment_id("123") == newest
    assert store.get_artifact_by_attachment_id("123", snapshot_id=old["snapshot_id"]) == older


def test_scoped_lookup_uses_insertion_order_for_same_second_rows(store, monkeypatch):
    snapshot = store.create_snapshot(issue_id=1)
    _artifact(store, snapshot, monkeypatch, "art_z")
    newest = _artifact(store, snapshot, monkeypatch, "art_a")

    assert store.get_artifact_by_attachment_id("123", snapshot_id=snapshot["snapshot_id"]) == newest
    assert store.get_artifact_by_attachment_id("123") == newest


def test_lookup_does_not_cross_attachment_or_snapshot_scope(store, monkeypatch):
    snapshot = store.create_snapshot(issue_id=1)
    _artifact(store, snapshot, monkeypatch, "art_a")

    assert store.get_artifact_by_attachment_id("456") is None
    assert store.get_artifact_by_attachment_id("123", snapshot_id="ev_missing") is None


def _ready_snapshot(store: EvidenceStore, **fields):
    snapshot = store.create_snapshot(issue_id=648526, download_policy="all")
    store.update_snapshot(
        snapshot["snapshot_id"],
        status="ready",
        content_sha256="a" * 64,
        **fields,
    )
    return store.get_snapshot(snapshot["snapshot_id"])


def test_derivation_gap_keeps_original_ready_and_flags_derived_dimension(tmp_path):
    store = EvidenceStore(tmp_path / "owner")
    snapshot = _ready_snapshot(store, error_json=[{"stage": "derive", "message": "OCR 失败"}])
    created = store.create_artifact(
        snapshot_id=snapshot["snapshot_id"], attachment_id="776657",
        filename="shot.png", original_filename="shot.png",
        content_type="image/png", kind="image",
    )
    store.update_artifact(
        created["artifact_id"],
        status="ready", derived_status="missing", derived_text_path="",
    )

    flags = snapshot_completeness(snapshot, store.list_artifacts(snapshot["snapshot_id"]))
    # 原件维度全绿；只有派生维度如实为缺口。
    assert flags["issue"] and flags["journals"] and flags["requested_downloads"]
    assert flags["derived_text"] is False
    artifact = store.get_artifact(created["artifact_id"])
    assert artifact["status"] == "ready"
    assert artifact["derived_status"] == "missing"
    assert artifact["text_available"] is False


def test_download_gap_pulls_snapshot_partial_and_requested_downloads_false(tmp_path):
    store = EvidenceStore(tmp_path / "owner")
    snapshot = store.create_snapshot(issue_id=648526, download_policy="all")
    store.update_snapshot(
        snapshot["snapshot_id"],
        status="partial",
        content_sha256="b" * 64,
        error_json=[{"stage": "download", "message": "附件 HTTP 500"}],
    )
    snapshot = store.get_snapshot(snapshot["snapshot_id"])

    flags = snapshot_completeness(snapshot, [])
    assert flags["issue"] and flags["journals"]
    assert flags["requested_downloads"] is False


def test_failed_snapshot_is_never_fully_complete(tmp_path):
    store = EvidenceStore(tmp_path / "owner")
    snapshot = store.create_snapshot(issue_id=648526, download_policy="none")
    store.update_snapshot(
        snapshot["snapshot_id"],
        status="failed",
        error_json=[{"stage": "issue", "message": "Redmine 不可达"}],
    )
    snapshot = store.get_snapshot(snapshot["snapshot_id"])

    flags = snapshot_completeness(snapshot, [])
    assert flags["issue"] is False and flags["journals"] is False
    assert not all(flags.values())


def test_derived_warning_only_lives_in_derived_dimension(tmp_path):
    """derived_status=partial（索引不完整）不改变原件与下载维度。"""
    store = EvidenceStore(tmp_path / "owner")
    snapshot = _ready_snapshot(store, error_json=[{"stage": "derive", "message": "成员超限"}])
    flags = snapshot_completeness(snapshot, [])

    assert flags["issue"] and flags["requested_downloads"]
    assert flags["derived_text"] is False
    # 对照：快照没有 derive 阶段错误时派生维度为 True。
    clean = _ready_snapshot(store)
    assert snapshot_completeness(clean, [])["derived_text"] is True

if __name__ == "__main__":
    unittest.main()
