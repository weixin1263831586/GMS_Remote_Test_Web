from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gms_backup.py"


class BackupToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.home = self.root / "home"
        self.output = self.root / "backups"
        (self.project / "data/nested").mkdir(parents=True)
        (self.project / "configs").mkdir()
        (self.home / ".ssh").mkdir(parents=True)
        (self.project / "data/nested/value.txt").write_text(
            "original", encoding="utf-8"
        )
        (self.project / "configs/config_runtime.json").write_text(
            '{"runtime": true}\n', encoding="utf-8"
        )
        (self.project / "configs/runtime.json").write_text(
            "TOKEN=private\n", encoding="utf-8"
        )
        (self.home / ".ssh/gms_web_app_rsa").write_text(
            "private-key", encoding="utf-8"
        )
        os.chmod(self.home / ".ssh/gms_web_app_rsa", 0o600)
        database = self.project / "data/state.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
            connection.execute("INSERT INTO state VALUES ('durable')")
        self.key = self.root / "backup.key"
        self.key.write_bytes(base64.urlsafe_b64encode(os.urandom(32)) + b"\n")
        os.chmod(self.key, 0o600)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_tool(self, *arguments: str, expected: int = 0):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def create(self, keep: int = 14) -> Path:
        result = self.run_tool(
            "create",
            "--project-root",
            str(self.project),
            "--run-home",
            str(self.home),
            "--output-dir",
            str(self.output),
            "--key-file",
            str(self.key),
            "--keep",
            str(keep),
        )
        return Path(json.loads(result.stdout)["archive"])

    def test_create_verify_and_restore_round_trip(self) -> None:
        archive = self.create()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.run_tool(
            "verify", "--archive", str(archive), "--key-file", str(self.key)
        )

        (self.project / "data/nested/value.txt").write_text(
            "mutated", encoding="utf-8"
        )
        (self.project / "configs/config_runtime.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (self.home / ".ssh/gms_web_app_rsa").write_text(
            "changed", encoding="utf-8"
        )
        self.run_tool(
            "restore",
            "--archive",
            str(archive),
            "--project-root",
            str(self.project),
            "--run-home",
            str(self.home),
            "--key-file",
            str(self.key),
            "--confirm",
            "RESTORE",
        )
        self.assertEqual(
            (self.project / "data/nested/value.txt").read_text(encoding="utf-8"),
            "original",
        )
        self.assertEqual(
            (self.project / "configs/config_runtime.json").read_text(
                encoding="utf-8"
            ),
            '{"runtime": true}\n',
        )
        self.assertEqual(
            (self.home / ".ssh/gms_web_app_rsa").read_text(encoding="utf-8"),
            "private-key",
        )
        with sqlite3.connect(self.project / "data/state.sqlite3") as connection:
            value = connection.execute("SELECT value FROM state").fetchone()[0]
        self.assertEqual(value, "durable")

    def test_corruption_and_wrong_key_are_rejected(self) -> None:
        archive = self.create()
        corrupted = self.root / "corrupted.gmsbak"
        payload = bytearray(archive.read_bytes())
        payload[len(payload) // 2] ^= 0x01
        corrupted.write_bytes(payload)
        self.run_tool(
            "verify",
            "--archive",
            str(corrupted),
            "--key-file",
            str(self.key),
            expected=1,
        )

        wrong_key = self.root / "wrong.key"
        wrong_key.write_bytes(base64.urlsafe_b64encode(os.urandom(32)) + b"\n")
        os.chmod(wrong_key, 0o600)
        self.run_tool(
            "verify",
            "--archive",
            str(archive),
            "--key-file",
            str(wrong_key),
            expected=1,
        )

    def test_retention_only_keeps_requested_archives(self) -> None:
        self.create(keep=1)
        latest = self.create(keep=1)
        self.assertEqual(list(self.output.glob("*.gmsbak")), [latest])


if __name__ == "__main__":
    unittest.main()
