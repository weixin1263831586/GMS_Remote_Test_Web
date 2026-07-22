from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paramiko

from foundation.ssh_security import (
    scan_ssh_host_keys,
    trust_scanned_ssh_host_keys,
)


class SshHostEnrollmentTests(unittest.TestCase):
    def test_only_freshly_scanned_key_is_persisted(self):
        key = paramiko.RSAKey.generate(1024)
        scan_line = f"host.example {key.get_name()} {key.get_base64()}\n"
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=scan_line, stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"GMS_SSH_KNOWN_HOSTS": str(Path(tmp) / "known_hosts")},
        ), patch("foundation.ssh_security.subprocess.run", return_value=completed):
            scanned = scan_ssh_host_keys("host.example", 2222)
            trusted = trust_scanned_ssh_host_keys(
                "host.example", 2222, scanned
            )
            path = Path(tmp) / "known_hosts"

            self.assertEqual(trusted, scanned)
            self.assertIn("[host.example]:2222 ssh-rsa ", path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_client_supplied_key_must_match_new_scan(self):
        key = paramiko.RSAKey.generate(1024)
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"host.example {key.get_name()} {key.get_base64()}\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"GMS_SSH_KNOWN_HOSTS": str(Path(tmp) / "known_hosts")},
        ), patch("foundation.ssh_security.subprocess.run", return_value=completed), self.assertRaises(ValueError):
            trust_scanned_ssh_host_keys(
                "host.example",
                22,
                [{
                    "key_type": "ssh-rsa",
                    "public_key": "forged",
                    "fingerprint": "SHA256:forged",
                }],
            )


if __name__ == "__main__":
    unittest.main()
