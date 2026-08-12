from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts_local" / "repair_audit_chain.py"


def _sign(key: bytes, record: dict) -> dict:
    signed = dict(record)
    payload = json.dumps(
        signed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signed["record_hash"] = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return signed


class AuditRepairScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.log_path = self.root / "security_audit.json"
        self.key_path = self.root / "audit_hmac.key"
        self.key = b"test-audit-key-material-32-bytes!!"
        self.key_path.write_bytes(self.key)
        self.key_path.chmod(0o600)

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["GMS_AUDIT_HMAC_KEY_FILE"] = str(self.key_path)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--log-path", str(self.log_path), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_records(self, records: list[dict]) -> bytes:
        payload = b"".join(
            json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
            for record in records
        )
        self.log_path.write_bytes(payload)
        return payload

    def _assert_valid_chain(self) -> list[dict]:
        records = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]
        previous = "GENESIS"
        for record in records:
            self.assertEqual(record["previous_hash"], previous)
            expected = record["record_hash"]
            unsigned = {key: value for key, value in record.items() if key != "record_hash"}
            actual = hmac.new(
                self.key,
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            self.assertTrue(hmac.compare_digest(expected, actual))
            previous = expected
        return records

    def test_default_mode_only_verifies_and_preserves_broken_log(self):
        first = _sign(self.key, {"id": "1", "previous_hash": "GENESIS"})
        second = _sign(self.key, {"id": "2", "previous_hash": "OTHER"})
        original = self._write_records([first, second])

        result = self._run()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.log_path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.corrupt-*")), [])

    def test_explicit_rebuild_preserves_backup_and_records_recovery(self):
        first = _sign(self.key, {"id": "1", "previous_hash": "GENESIS"})
        second = _sign(self.key, {"id": "2", "previous_hash": "OTHER"})
        original = self._write_records([first, second])

        result = self._run("--rebuild")

        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(self.root.glob("security_audit.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        records = self._assert_valid_chain()
        self.assertEqual(records[-1]["operation"], "audit_chain_rebuild")
        self.assertEqual(
            records[-1]["details"]["source_sha256"],
            hashlib.sha256(original).hexdigest(),
        )

    def test_invalid_hmac_requires_separate_authorization_flag(self):
        tampered = _sign(self.key, {"id": "1", "previous_hash": "GENESIS"})
        tampered["operation"] = "changed-after-signing"
        original = self._write_records([tampered])

        refused = self._run("--rebuild")
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(self.log_path.read_bytes(), original)

        rebuilt = self._run("--rebuild", "--allow-invalid-hmac")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        records = self._assert_valid_chain()
        self.assertEqual(records[-1]["details"]["invalid_hmacs"], 1)

    def test_invalid_json_requires_separate_drop_flag(self):
        valid = _sign(self.key, {"id": "1", "previous_hash": "GENESIS"})
        original = self._write_records([valid]) + b"not-json\n"
        self.log_path.write_bytes(original)

        refused = self._run("--rebuild")
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(self.log_path.read_bytes(), original)

        rebuilt = self._run("--rebuild", "--drop-invalid-json")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        records = self._assert_valid_chain()
        self.assertEqual(records[-1]["details"]["dropped_invalid_json_lines"], 1)


if __name__ == "__main__":
    unittest.main()
