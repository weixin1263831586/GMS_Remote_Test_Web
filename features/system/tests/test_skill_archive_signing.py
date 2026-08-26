from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from features.system.skill_archive_signing import (
    sign_skill_archive,
    skill_verify_key_b64,
)


class SkillArchiveSigningTests(unittest.TestCase):
    def test_configured_ed25519_key_signs_and_exports_matching_public_key(self):
        private_key = Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as temporary:
            key_path = Path(temporary) / "skill-signing.pem"
            key_path.write_bytes(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ))
            with patch.dict(
                "os.environ",
                {"GMS_SKILL_SIGNING_KEY_FILE": str(key_path)},
            ):
                signature = base64.b64decode(sign_skill_archive(b"archive"))
                public_key = serialization.load_pem_public_key(
                    base64.b64decode(skill_verify_key_b64())
                )

        public_key.verify(signature, b"archive")

    def test_signing_is_disabled_without_key(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch.dict("os.environ", {"GMS_SKILL_SIGNING_KEY_FILE": ""}):
                self.assertEqual(sign_skill_archive(b"archive"), "")
                self.assertEqual(skill_verify_key_b64(), "")


if __name__ == "__main__":
    unittest.main()
