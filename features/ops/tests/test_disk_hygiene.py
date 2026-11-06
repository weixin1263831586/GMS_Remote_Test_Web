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

        result = disk_hygiene.run_cleanup(
            dry_run=False, include_stage=True, stage_max_age_days=7
        )
        self.assertGreaterEqual(result["removed_files"], 1)

        self.assertFalse(old_img.exists())
        self.assertTrue(fresh_img.exists())

    def test_dry_run_reports_candidates_without_deleting(self):
        data_root = self._with_fake_data_root()
        old_img = self._stage_file(data_root, "run-3", "update.img", age_days=30)

        preview = disk_hygiene.run_cleanup(dry_run=True)

        self.assertTrue(old_img.exists())
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["freed_bytes"], 0)
        self.assertEqual(preview["candidate_count"], 1)
        self.assertIn("update.img", preview["candidates"][0]["path"])

    def test_preview_lists_largest_dirs(self):
        data_root = self._with_fake_data_root()
        self._stage_file(data_root, "run-4", "update.img", age_days=0)
        overview = disk_hygiene.preview_cleanup()
        names = [item["path"] for item in overview["largest_dirs"]]
        self.assertIn("automation", names)


if __name__ == "__main__":
    unittest.main()
