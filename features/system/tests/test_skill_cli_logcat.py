"""gms-rt-devices-logcat CLI tests: stub adb drives the real helper process."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from features.system.tests.skill_cli_mock_server import ApiHandler as _ApiHandler


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "skills" / "gms-remote-test" / "scripts" / "gms-remote-test.sh"


class DevicesLogcatTests(unittest.TestCase):
    """gms-rt-devices-logcat: 用 PATH 中的 stub adb 驱动真实 CLI 进程。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _run(
        self,
        *arguments: str,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as temporary:
            stub_dir = Path(temporary) / "bin"
            stub_dir.mkdir()
            log_path = Path(temporary) / "adb-calls.log"
            # stub adb: 记录参数; devices 列出 SERIAL-1; 其余调用输出假日志行。
            (stub_dir / "adb").write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' \"$*\" >> '" + str(log_path) + "'\n"
                "if [ \"$1\" = \"devices\" ]; then printf 'SERIAL-1\\tdevice\\n'; exit 0; fi\n"
                "printf '09-07 10:00:00.123 I/TEST( 1): fake logcat line\\n'\n",
                encoding="utf-8",
            )
            (stub_dir / "adb").chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": temporary,
                    "PATH": f"{stub_dir}:{os.environ['PATH']}",
                    "GMS_REMOTE_TEST_SERVER": (
                        f"http://127.0.0.1:{self.server.server_port}"
                    ),
                    "GMS_AUTH_COOKIE_JAR": str(Path(temporary) / "session.cookies"),
                    "NO_COLOR": "1",
                }
            )
            result = subprocess.run(
                ["bash", str(HELPER), *arguments],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            calls = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        return result, calls

    def test_non_interactive_logcat_dumps_with_v_time(self):
        result, calls = self._run(
            "gms-rt-devices-logcat", "SERIAL-1", "--json", "--non-interactive"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["ok"])
        # 设备端命令固定为 logcat -v time, 无 dump 标志时自动附加 -d。
        self.assertIn("-s SERIAL-1 shell logcat -v time -d", calls)
        self.assertIn("fake logcat line", envelope["output"])

    def test_existing_dump_flag_is_kept_and_args_pass_through(self):
        result, calls = self._run(
            "gms-rt-devices-logcat",
            "SERIAL-1",
            "-b",
            "crash",
            "-t",
            "100",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "-s SERIAL-1 shell logcat -v time -b crash -t 100", calls
        )
        self.assertNotIn("time -d -b", calls)

    def test_clear_flag_runs_logcat_c_first(self):
        result, calls = self._run(
            "gms-rt-devices-logcat", "SERIAL-1", "-c", "--json", "--non-interactive"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["ok"])
        # 先清空缓冲, 再以 -v time dump 抓取; -c 本身不进入抓取参数。
        self.assertIn("-s SERIAL-1 shell logcat -c", calls)
        self.assertIn("-s SERIAL-1 shell logcat -v time -d", calls)
        self.assertIn("fake logcat line", envelope["output"])

    def test_clear_flag_combines_with_other_args(self):
        result, calls = self._run(
            "gms-rt-devices-logcat",
            "SERIAL-1",
            "-c",
            "-b",
            "crash",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-s SERIAL-1 shell logcat -v time -d -b crash", calls)

    def test_file_flag_is_a_usage_error(self):
        result, _calls = self._run(
            "gms-rt-devices-logcat", "SERIAL-1", "-f", "--json", "--non-interactive"
        )

        self.assertEqual(result.returncode, 2)
        envelope = json.loads(result.stdout)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["exit_code"], 2)

    def test_metacharacters_are_rejected_when_non_interactive(self):
        result, _calls = self._run(
            "gms-rt-devices-logcat",
            "SERIAL-1",
            "a;reboot",
            "--json",
            "--non-interactive",
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_missing_device_is_a_usage_error(self):
        result, _calls = self._run(
            "gms-rt-devices-logcat", "--json", "--non-interactive"
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])


