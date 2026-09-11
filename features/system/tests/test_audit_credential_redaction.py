"""Unit tests: credential-body audit redaction path matching.

These assertions live next to the implementation (features/system) so the
auth acceptance tests don't need to import system feature internals
(architecture dependency gate).
"""

from __future__ import annotations

import unittest

from features.system.security_audit_utils import (
    AUDIT_CREDENTIAL_BODY_REDACTED,
    _is_credential_body_path,
)


class CredentialBodyPathTests(unittest.TestCase):
    def test_exact_credential_paths_are_redacted(self):
        self.assertTrue(_is_credential_body_path("/api/auth/agent-enroll"))
        self.assertTrue(_is_credential_body_path("/api/auth/agent-enrollment-codes"))

    def test_business_and_neighbour_paths_are_not(self):
        # 一刀切屏蔽所有 code 字段是审核明确拒绝的方案；路径必须精确。
        self.assertFalse(_is_credential_body_path("/api/cluster/jobs"))
        self.assertFalse(_is_credential_body_path("/api/auth/login"))
        self.assertFalse(_is_credential_body_path("/api/auth/agent-enroll/extra"))
        self.assertFalse(_is_credential_body_path("/api/auth/agent-enrollx"))
        self.assertFalse(_is_credential_body_path(""))

    def test_placeholder_marker_shape(self):
        self.assertTrue(AUDIT_CREDENTIAL_BODY_REDACTED["redacted"])
        self.assertEqual(
            AUDIT_CREDENTIAL_BODY_REDACTED["reason"], "认证凭据正文不记录"
        )


if __name__ == "__main__":
    unittest.main()
