from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.system.security_audit import SecurityAuditLogger


class SecurityAuditIntegrityTests(unittest.TestCase):
    def test_signed_chain_detects_record_tampering(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"GMS_AUDIT_HMAC_KEY": "a" * 64, "GMS_ENV": "production"},
        ):
            path = Path(tmp) / "audit.jsonl"
            audit = SecurityAuditLogger(str(path))
            first = audit.log_event({"operation": "lease.acquire", "status_code": 200})
            second = audit.log_event({"operation": "lease.release", "status_code": 200})

            verified = audit.verify_chain()
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["signed_records"], 2)
            self.assertEqual(second["previous_hash"], first["record_hash"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            lines = path.read_text(encoding="utf-8").splitlines()
            altered = json.loads(lines[0])
            altered["operation"] = "lease.force-release"
            lines[0] = json.dumps(altered, ensure_ascii=False, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            invalid = audit.verify_chain()
            self.assertFalse(invalid["valid"])
            self.assertEqual(invalid["line"], 1)

    def test_append_rejects_a_different_active_key_without_changing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            audit = SecurityAuditLogger(str(path))
            with patch.dict(
                "os.environ",
                {"GMS_AUDIT_HMAC_KEY": "a" * 64, "GMS_ENV": "production"},
            ):
                audit.log_event({"operation": "production-event"})
                original = path.read_bytes()

            with patch.dict(
                "os.environ",
                {"GMS_AUDIT_HMAC_KEY": "b" * 64, "GMS_ENV": "production"},
            ), self.assertRaisesRegex(
                RuntimeError,
                "active audit key does not match",
            ):
                audit.log_event({"operation": "foreign-key-event"})

            self.assertEqual(path.read_bytes(), original)
            with patch.dict(
                "os.environ",
                {"GMS_AUDIT_HMAC_KEY": "a" * 64, "GMS_ENV": "production"},
            ):
                self.assertTrue(audit.verify_chain()["valid"])

    def test_unsigned_legacy_prefix_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"GMS_AUDIT_HMAC_KEY": "a" * 64, "GMS_ENV": "production"},
        ):
            path = Path(tmp) / "audit.jsonl"
            path.write_text(
                json.dumps({"operation": "legacy-event"}) + "\n",
                encoding="utf-8",
            )
            audit = SecurityAuditLogger(str(path))

            result = audit.verify_chain()

            self.assertFalse(result["valid"])
            self.assertEqual(result["line"], 1)
            self.assertEqual(result["error"], "unsigned audit record")

    def test_device_claim_fencing_fields_are_not_redacted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"GMS_AUDIT_HMAC_KEY": "a" * 64, "GMS_ENV": "production"},
        ):
            audit = SecurityAuditLogger(str(Path(tmp) / "audit.jsonl"))

            record = audit.log_event({
                "operation": "device.reboot",
                "device_claims": [{
                    "lease_id": "claim-1",
                    "generation": 7,
                    "owner_id": "owner-1",
                }],
            })

            self.assertEqual(
                record["device_claims"][0],
                {
                    "lease_id": "claim-1",
                    "generation": 7,
                    "owner_id": "owner-1",
                },
            )

    def test_recent_event_reads_are_bounded_to_tail_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"GMS_AUDIT_HMAC_KEY": "a" * 64, "GMS_ENV": "production"},
        ):
            audit = SecurityAuditLogger(
                str(Path(tmp) / "audit.jsonl"),
                max_read_lines=3,
            )
            for index in range(8):
                audit.log_event({"operation": f"事件-{index}", "status_code": 200})

            result = audit.read_events(limit=10)

            self.assertEqual(
                [record["operation"] for record in result["records"]],
                ["事件-7", "事件-6", "事件-5"],
            )
            self.assertEqual(result["stats"]["total"], 3)

    def test_log_rotates_and_preserves_chain_when_size_cap_exceeded(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"GMS_AUDIT_HMAC_KEY": "a" * 64, "GMS_ENV": "production"},
        ):
            path = Path(tmp) / "audit.jsonl"
            audit = SecurityAuditLogger(str(path))
            # Drive the cap down so we can exercise rotation quickly.
            audit.MAX_LOG_BYTES = 4 * 1024
            audit.ROTATE_KEEP_BYTES = 1 * 1024
            for index in range(200):
                audit.log_event({
                    "operation": f"event-{index}",
                    "status_code": 200,
                    "padding": "x" * 80,
                })

            # File must be back under the cap after rotation.
            self.assertLess(path.stat().st_size, audit.MAX_LOG_BYTES + 1024)
            # A rotated backup of the pre-truncation file must exist.
            rotated = list(Path(tmp).glob("audit.jsonl.rotated*"))
            self.assertEqual(len(rotated), 1)
            # The post-rotation log must still verify as one signed chain.
            verified = audit.verify_chain()
            self.assertTrue(verified["valid"])
            self.assertGreater(verified["signed_records"], 0)
            # New events can still be appended onto the rotated chain.
            tail = audit.log_event({"operation": "post-rotate", "status_code": 200})
            self.assertEqual(tail["previous_hash"], verified["head_hash"])
            again = audit.verify_chain()
            self.assertTrue(again["valid"])


class AuditNoisePathTests(unittest.TestCase):
    def test_high_frequency_readonly_polling_is_not_audited(self):
        """浏览器侧任务/命令高频只读轮询不进常规审计（失败仍记录）。"""
        from features.system.security_audit_utils import should_audit_request

        for path in (
            "/api/cluster/jobs/job-abc123",
            "/api/cluster/jobs/job-abc123/events",
            "/api/cluster/commands/cmd-abc123",
        ):
            self.assertFalse(
                should_audit_request(path, "web", "GET"),
                f"polling path should be skipped: {path}",
            )

    def test_write_and_non_web_sources_still_audited(self):
        from features.system.security_audit_utils import should_audit_request

        # 写操作仍需审计
        self.assertTrue(
            should_audit_request("/api/cluster/jobs/job-abc123", "web", "DELETE")
        )
        # 非 web 来源（如 Worker）仍需审计
        self.assertTrue(
            should_audit_request("/api/cluster/jobs/job-abc123", "worker", "GET")
        )
        # 相似但不同的路径不受影响
        self.assertTrue(
            should_audit_request("/api/cluster/jobs/job-abc123/artifacts", "web", "GET")
        )


if __name__ == "__main__":
    unittest.main()
