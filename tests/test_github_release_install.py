"""Offline tests for the GitHub Release copy-and-run installer.

Uses a mocked GitHub HTTPS downloader, actual temporary GPG signing keys,
actual tar extraction/checksums, and no root/systemd/network access.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/release/install_from_github.sh"
PUBLISHER = ROOT / "scripts/release/publish_github_release.sh"
REPO = "weixin1263831586/GMS_Remote_Test_Web"
ARCHIVE = "gms-web-app-1.0.0-ubuntu22.04-x86_64.tar.gz"


def run(args, *, env=None):
    return subprocess.run(args, env=env, text=True, capture_output=True, check=False)


@unittest.skipUnless(shutil.which("bash"), "bash unavailable")
class GitHubReleaseInstallContracts(unittest.TestCase):
    def test_shell_syntax_and_help(self):
        for path in (INSTALLER, PUBLISHER):
            with self.subTest(path=path):
                self.assertEqual(run(["bash", "-n", str(path)]).returncode, 0)
        output = run(["bash", str(INSTALLER), "--help"])
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn("--download-only", output.stdout)

    def test_invalid_tag_and_fingerprint_refuse_before_network_or_sudo(self):
        for arguments in (
            ["--tag", "../main", "--fingerprint", "0" * 40],
            ["--fingerprint", "not-a-valid-publisher-fingerprint"],
        ):
            with self.subTest(args=arguments):
                result = run(["bash", str(INSTALLER), *arguments])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refused:", result.stderr)

    def test_readme_includes_copiable_command_and_no_git_clone(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("scripts/release/install_from_github.sh && bash gms-install.sh", readme)
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("git clone", installer)
        self.assertIn("sudo bash ./install.sh", installer)
        self.assertIn("VALIDSIG", installer)
        self.assertIn("sha256sum -c", installer)
        self.assertIn("publisher.asc", installer)

    @unittest.skipUnless(shutil.which("gpg") and shutil.which("tar"), "GPG/tar required")
    def test_mocked_github_release_installer_passes_only_with_valid_signer(self):
        with tempfile.TemporaryDirectory(prefix="gms-installer-") as temp:
            base = Path(temp)
            gnupg = base / "publisher-gnupg"
            gnupg.mkdir(mode=0o700)
            env = {**os.environ, "GNUPGHOME": str(gnupg)}
            key_id = "GMS installer test <test@example.invalid>"
            setup = run([
                "gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                "--quick-generate-key", key_id, "ed25519", "sign", "1d",
            ], env=env)
            self.assertEqual(setup.returncode, 0, setup.stderr)
            keys = run(["gpg", "--batch", "--with-colons", "--fingerprint",
                        "--list-keys", key_id], env=env)
            self.assertEqual(keys.returncode, 0, keys.stderr)
            fingerprint = next(
                row.split(":")[9] for row in keys.stdout.splitlines()
                if row.startswith("fpr:")
            )
            self.assertEqual(len(fingerprint), 40)

            release_files = base / "assets"
            release_files.mkdir()
            archive = release_files / ARCHIVE
            installer = b"#!/usr/bin/env bash\nexit 0\n"
            manifest = json.dumps({
                "format": "gms-compiled-v1", "version": "1.0.0",
            }).encode()
            manifest_checksum = (
                hashlib.sha256(installer).hexdigest() + "  install.sh\n"
                + hashlib.sha256(manifest).hexdigest() + "  manifest.json\n"
            ).encode()
            with tarfile.open(archive, "w:gz") as tar:
                for name, payload in (
                    ("gms-web-app/install.sh", installer),
                    ("gms-web-app/manifest.json", manifest),
                    ("gms-web-app/manifest.sha256", manifest_checksum),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    member.mode = 0o755 if name.endswith("/install.sh") else 0o644
                    tar.addfile(member, io.BytesIO(payload))
            (release_files / (ARCHIVE + ".sha256")).write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest()
                + "  " + ARCHIVE + "\n", encoding="utf-8",
            )
            signature = run([
                "gpg", "--batch", "--armor", "--detach-sign", "--local-user",
                key_id, "--output", str(release_files / (ARCHIVE + ".sig")),
                str(archive),
            ], env=env)
            self.assertEqual(signature.returncode, 0, signature.stderr)
            public = subprocess.run(
                ["gpg", "--batch", "--armor", "--export", key_id],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            )
            (release_files / "publisher.asc").write_bytes(public.stdout)
            names = (ARCHIVE, ARCHIVE + ".sha256", ARCHIVE + ".sig", "publisher.asc")
            (release_files / "release.json").write_text(json.dumps({
                "tag_name": "gms-v1.0.0", "draft": False, "prerelease": False,
                "assets": [
                    {"name": name, "browser_download_url":
                        f"https://github.com/{REPO}/releases/download/gms-v1.0.0/{name}"}
                    for name in names
                ],
            }), encoding="utf-8")

            mock_bin = base / "mockbin"
            mock_bin.mkdir()
            fake_curl = mock_bin / "curl"
            fake_curl.write_text(r"""#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path
args = sys.argv[1:]
dest = args[args.index("-o") + 1]
url = next(arg for arg in args if arg.startswith("https://"))
name = "release.json" if url.endswith("/latest") else url.rsplit("/", 1)[-1]
shutil.copyfile(Path(os.environ["GMS_TEST_RELEASE_ASSETS"]) / name, dest)
""")
            fake_curl.chmod(0o755)
            clean_env = {**os.environ,
                         "PATH": str(mock_bin) + os.pathsep + os.environ.get("PATH", ""),
                         "GMS_TEST_RELEASE_ASSETS": str(release_files)}
            valid = run(["bash", str(INSTALLER), "--fingerprint",
                         fingerprint, "--download-only"], env=clean_env)
            self.assertEqual(valid.returncode, 0, valid.stderr + valid.stdout)
            self.assertIn("validation passed", valid.stdout)
            bad = run(["bash", str(INSTALLER), "--fingerprint",
                       "0" * 40, "--download-only"], env=clean_env)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("fingerprint does not match", bad.stderr)

            # A tampered asset must fail before any installer is invoked.
            archive.write_bytes(archive.read_bytes() + b"tampered")
            tampered = run(["bash", str(INSTALLER), "--fingerprint",
                            fingerprint, "--download-only"], env=clean_env)
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("checksum mismatch", tampered.stderr)


if __name__ == "__main__":
    unittest.main()
