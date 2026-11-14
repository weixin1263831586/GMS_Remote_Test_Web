"""Disk hygiene tests (phase 3): boundary and mtime-based sweeps."""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from features.ops import disk_hygiene


class DiskHygieneTests(unittest.TestCase):
    def _with_fake_data_root(self):
        tmp = TemporaryDirectory()
        data_root = Path(tmp.name) / "data"
        data_root.mkdir()
        patcher = mock.patch.object(
            disk_hygiene, "settings", mock.MagicMock(data_root=data_root)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(tmp.cleanup)
        return data_root

    def _stage_file(self, data_root: Path, run_id: str, name: str, age_days: float) -> Path:
        directory = data_root / "automation" / "artifact-stage" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        target.write_bytes(b"firmware" * 1024)
        stamp = time.time() - age_days * 86400
        os.utime(target, (stamp, stamp))
        return target

    def _register_active_runs(self, run_ids: set[str]):
        """注册非终态 run 提供者（组合根语义）；未注册时 stage 清理 fail-closed。"""
        from foundation import automation_port

        patcher = mock.patch.object(
            automation_port, "_active_run_ids_provider", lambda: run_ids
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_pass_removes_part_and_upload_temp_but_keeps_stage(self):
        data_root = self._with_fake_data_root()
        old_img = self._stage_file(data_root, "run-1", "update.img", age_days=30)
        old_part = self._stage_file(data_root, "run-1", ".update.img.abc.part", age_days=5)
        # foundation.uploads resolves its own settings reference (fresh import
        # inside the function), so it does not follow the patched data_root;
        # point GMS_UPLOAD_DIR at the sandbox instead.
        upload_root = data_root / "uploads" / "gms_uploads" / "client"
        upload_root.mkdir(parents=True, exist_ok=True)
        stale = upload_root / "session-old"
        stale.mkdir()
        stale_chunk = stale / "chunk.bin"
        stale_chunk.write_bytes(b"x")
        stale_stamp = time.time() - 30 * 86400
        os.utime(stale, (stale_stamp, stale_stamp))
        os.utime(stale_chunk, (stale_stamp, stale_stamp))

        with mock.patch.dict("os.environ", {"GMS_UPLOAD_DIR": str(data_root / "uploads")}):
            result = disk_hygiene.run_cleanup(dry_run=False, include_stage=False)

        self.assertTrue(old_img.exists(), "staged firmware must survive the auto pass")
        self.assertFalse(old_part.exists())
        self.assertFalse(stale.exists())
        self.assertGreaterEqual(result["removed_files"], 2)
        self.assertFalse(result["dry_run"])
        self.assertFalse(result["include_stage"])

    def test_admin_pass_with_stage_flag_removes_stale_firmware(self):
        data_root = self._with_fake_data_root()
        old_img = self._stage_file(data_root, "run-2", "update.img", age_days=30)
        fresh_img = self._stage_file(data_root, "run-2", "fresh.img", age_days=1)
        self._register_active_runs(set())

        result = disk_hygiene.run_cleanup(
            dry_run=False, include_stage=True, stage_max_age_days=7
        )
        self.assertGreaterEqual(result["removed_files"], 1)

        self.assertFalse(old_img.exists())
        self.assertTrue(fresh_img.exists())

    def test_active_upload_session_is_never_removed(self):
        # 父目录 mtime 不随深层写入更新，旧 owner 目录下的
        # 活跃会话曾连同目录一起被删；现在以子树最新 mtime 判定。
        data_root = self._with_fake_data_root()
        upload_root = data_root / "uploads" / "gms_uploads" / "owner-old"
        active = upload_root / "session-active"
        active.mkdir(parents=True)
        fresh_chunk = active / "chunk.bin"
        fresh_chunk.write_bytes(b"x")
        stale = upload_root / "session-stale"
        stale.mkdir()
        stale_chunk = stale / "chunk.bin"
        stale_chunk.write_bytes(b"x")
        old = time.time() - 30 * 86400
        os.utime(upload_root, (old, old))
        os.utime(stale, (old, old))
        os.utime(stale_chunk, (old, old))
        # 活跃会话目录本身保持旧 mtime，只有深层 chunk 是新的。
        os.utime(active, (old, old))

        with mock.patch.dict("os.environ", {"GMS_UPLOAD_DIR": str(data_root / "uploads")}):
            disk_hygiene.run_cleanup(dry_run=False, include_stage=False)

        self.assertTrue(active.exists(), "active session must survive")
        self.assertTrue(fresh_chunk.exists())
        self.assertFalse(stale.exists())

    def test_admin_stage_pass_skips_runs_that_are_not_terminal(self):
        # 暂存固件清理必须核对运行状态，非终态 run 引用的
        # 镜像 fail-closed 跳过。
        data_root = self._with_fake_data_root()
        protected = self._stage_file(data_root, "run-active", "update.img", age_days=30)
        free = self._stage_file(data_root, "run-done", "update.img", age_days=30)

        with mock.patch.object(
            disk_hygiene, "_protected_stage_run_ids",
            return_value={"run-active"},
        ):
            disk_hygiene.run_cleanup(
                dry_run=False, include_stage=True, stage_max_age_days=7
            )

        self.assertTrue(protected.exists(), "referenced image must survive")
        self.assertFalse(free.exists())

    def test_stage_lookup_failure_fails_closed(self):
        # 引用查询异常时不得清理任何 stage 文件（fail closed）。
        data_root = self._with_fake_data_root()
        img = self._stage_file(data_root, "run-x", "update.img", age_days=30)

        with mock.patch.object(
            disk_hygiene.settings, "data_root", data_root
        ), mock.patch.object(
            disk_hygiene, "_protected_stage_run_ids",
            return_value=None,
        ):
            preview = disk_hygiene.preview_cleanup(stage_max_age_days=7)
            result = disk_hygiene.run_cleanup(
                dry_run=False, include_stage=True, stage_max_age_days=7
            )

        self.assertTrue(img.exists())
        self.assertEqual(result["removed_files"], 0)
        self.assertTrue(
            all(item["deletable"] is False for item in preview["candidates"])
        )

    def test_preview_matches_execution_candidates(self):
        # 预览与实际删除共用候选清单，deletable 标记一致。
        data_root = self._with_fake_data_root()
        keep = self._stage_file(data_root, "run-keep", "a.img", age_days=30)
        drop = self._stage_file(data_root, "run-drop", "b.img", age_days=30)

        with mock.patch.object(
            disk_hygiene, "_protected_stage_run_ids", return_value={"run-keep"}
        ):
            preview = disk_hygiene.preview_cleanup(stage_max_age_days=7)
            flags = {
                Path(item["path"]).parent.name: item["deletable"]
                for item in preview["candidates"]
            }
            self.assertEqual(flags, {"run-keep": False, "run-drop": True})
            self.assertEqual(preview["protected_count"], 1)
            self.assertEqual(preview["deletable_count"], 1)

            result = disk_hygiene.run_cleanup(
                dry_run=False, include_stage=True, stage_max_age_days=7
            )
        self.assertTrue(keep.exists())
        self.assertFalse(drop.exists())
        self.assertEqual(result["removed_files"], 1)

    def test_dry_run_reports_candidates_without_deleting(self):
        data_root = self._with_fake_data_root()
        old_img = self._stage_file(data_root, "run-3", "update.img", age_days=30)
        self._register_active_runs(set())

        preview = disk_hygiene.run_cleanup(dry_run=True)

        self.assertTrue(old_img.exists())
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["freed_bytes"], 0)
        self.assertEqual(preview["candidate_count"], 1)
        self.assertIn("update.img", preview["candidates"][0]["path"])

    def test_preview_lists_largest_dirs(self):
        data_root = self._with_fake_data_root()
        self._stage_file(data_root, "run-4", "update.img", age_days=0)
        self._register_active_runs(set())
        overview = disk_hygiene.preview_cleanup()
        names = [item["path"] for item in overview["largest_dirs"]]
        self.assertIn("automation", names)


if __name__ == "__main__":
    unittest.main()
