"""``gms-rt-apk-analyze`` JSON 输出必须能在 jq 1.6 上编译。

2026-10-08 实机取证发现：成功分支的 jq 模板把保留字 ``module`` 同时用作
对象键和 ``--arg`` 变量名，jq 1.6 直接报
``syntax error, unexpected module``，task_id 被吞掉。服务端 owner 修复之前
该分支从不执行，所以模板缺陷一直潜伏。此测试用 curl 函数桩走真实脚本，
断言成功路径输出可解析的 JSON 信封。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "runtime"
if not RUNTIME_ROOT.is_dir():
    RUNTIME_ROOT = PACKAGE_ROOT / "scripts"
SCRIPT = (RUNTIME_ROOT / "gms-remote-test.sh").as_posix()


class ApkAnalyzeJsonOutputTests(unittest.TestCase):
    def test_success_envelope_parses_with_jq_1_6(self):
        command = """
source "$1"
curl() {
  case "$*" in
    *"/test/suites/modules/apk"*)
      printf '%s\\n' '{"success":true,"data":{"module":"CtsSecurityTestCases","file_name":"CtsSecurityTestCases.apk","analyze_suite_path":"/suite","analyze_path":"testcases/CtsSecurityTestCases.apk"}}' 'HTTP_STATUS:200'
      ;;
    *"/test/suites/apk/analyze"*)
      printf '%s\\n' '{"success":true,"data":{"task_id":"t-fixture-1"}}' 'HTTP_STATUS:200'
      ;;
    *"/apk/analyze/t-fixture-1"*)
      printf '%s\\n' '{"success":true,"data":{"task_id":"t-fixture-1","status":"analyzing"}}' 'HTTP_STATUS:200'
      ;;
    *)
      printf '%s\\n' '{"success":false}' 'HTTP_STATUS:500'
      ;;
  esac
}
gms-rt-apk-analyze CtsSecurityTestCases
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("home", "config", "state", "data"):
                (root / name).mkdir()
            result = subprocess.run(
                ["bash", "-c", command, "bash", SCRIPT],
                capture_output=True,
                text=True,
                env={
                    **{key: value for key, value in os.environ.items() if not key.startswith("GMS_")},
                    "HOME": str(root / "home"),
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_DATA_HOME": str(root / "data"),
                    "GMS_REMOTE_TEST_SERVER": "https://fixture.invalid:5001",
                    "GMS_RT_OUTPUT": "json",
                    "GMS_RT_NON_INTERACTIVE": "1",
                },
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("jq: error", result.stderr + result.stdout)
        envelope = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(envelope["success"])
        self.assertEqual(envelope["task_id"], "t-fixture-1")
        self.assertEqual(envelope["module"], "CtsSecurityTestCases")
        self.assertEqual(envelope["status"], "analyzing")


if __name__ == "__main__":
    unittest.main()
