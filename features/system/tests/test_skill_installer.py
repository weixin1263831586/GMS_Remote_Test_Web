from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from features.system.api import _skill_directory


ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = ROOT / "skills" / "gms-remote-test"
INSTALLER = SKILL_DIR / "scripts" / "install.sh"


class SkillInstallerTests(unittest.TestCase):
    def test_skill_directory_rejects_path_traversal(self):
        self.assertIsNone(_skill_directory("../configs"))
        self.assertIsNone(_skill_directory("/tmp"))
        self.assertIsNone(_skill_directory("gms_remote_test"))

    def _create_skill_archive(self, archive_path: Path) -> None:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(SKILL_DIR.rglob("*")):
                if source.is_file():
                    relative = source.relative_to(SKILL_DIR)
                    archive.write(source, Path("gms-remote-test") / relative)

    def _run(self, command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_install_cli_and_repeatable_update_in_isolated_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            skills_dir = base / "codex" / "skills"
            bin_dir = base / "bin"
            runtime_bin_dir = base / "runtime-bin"
            profile = home / ".profile"
            archive_path = base / "gms-remote-test.zip"
            home.mkdir()
            self._create_skill_archive(archive_path)

            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "GMS_CODEX_SKILLS_DIR": str(skills_dir),
                    "GMS_BIN_DIR": str(bin_dir),
                    "GMS_RUNTIME_BIN_DIR": str(runtime_bin_dir),
                    "GMS_PROFILE_FILE": str(profile),
                    "GMS_REMOTE_TEST_SERVER": "https://controller.example:5001",
                    "GMS_SKILL_DOWNLOAD_URL": archive_path.as_uri(),
                    "GMS_INSTALL_INSECURE": "0",
                }
            )

            first = self._run(["bash", str(INSTALLER)], env)
            target = skills_dir / "gms-remote-test"
            dispatcher = runtime_bin_dir / "gms-rt-dispatcher"

            self.assertIn("Installed Skill:", first.stdout)
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue(dispatcher.is_file())
            self.assertFalse((bin_dir / "gms-rt").exists())
            self.assertFalse((bin_dir / "gms-remote-test").exists())
            self.assertTrue(dispatcher.stat().st_mode & stat.S_IXUSR)
            self.assertTrue((target / "scripts" / "install.sh").stat().st_mode & stat.S_IXUSR)
            self.assertIn(
                "export GMS_CURL_INSECURE=0",
                dispatcher.read_text(encoding="utf-8"),
            )

            helper_text = (target / "scripts" / "gms-remote-test.sh").read_text(
                encoding="utf-8"
            )
            command_names = set(
                re.findall(r"^(gms-rt-[a-z0-9-]+)\(\)", helper_text, re.MULTILINE)
            )
            command_names.add("gms-rt-update")
            for command_name in command_names:
                command_link = bin_dir / command_name
                self.assertTrue(command_link.is_symlink(), command_name)
                self.assertEqual(command_link.resolve(), dispatcher.resolve())

            help_result = self._run([str(bin_dir / "gms-rt-system-help")], env)
            self.assertIn("GMS Remote Test API Helper", help_result.stdout)
            self.assertIn("https://controller.example:5001", help_result.stdout)

            stale_link = bin_dir / "gms-rt-obsolete"
            stale_link.symlink_to(dispatcher)
            legacy_wrapper = bin_dir / "gms-rt"
            legacy_wrapper.write_text(
                "#!/usr/bin/env bash\n"
                "HELPER=/tmp/gms-remote-test/scripts/gms-remote-test.sh\n"
                "export GMS_SKILL_DOWNLOAD_URL=https://controller.example/skill\n",
                encoding="utf-8",
            )
            legacy_wrapper.chmod(0o755)
            legacy_alias = bin_dir / "gms-remote-test"
            legacy_alias.symlink_to(legacy_wrapper)
            obsolete = target / "obsolete-after-update"
            obsolete.write_text("remove me", encoding="utf-8")
            update_result = self._run([str(bin_dir / "gms-rt-update")], env)

            self.assertIn("Installed CLI Runtime:", update_result.stdout)
            self.assertFalse(obsolete.exists())
            self.assertFalse(stale_link.exists())
            self.assertFalse(legacy_wrapper.exists())
            self.assertFalse(legacy_alias.exists())
            self.assertEqual(
                profile.read_text(encoding="utf-8").count("# GMS Remote Test CLI"),
                1,
            )


if __name__ == "__main__":
    unittest.main()
