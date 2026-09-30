"""Tradefed 控制台启动预算测试。

本地控制台路径（execute_tradefed_command_local）不得因繁忙主机上的 JVM
冷启动超过旧硬编码 15s 而间歇性失败；预算可通过
GMS_TRADEFED_STARTUP_TIMEOUT 调整，且不允许配置为低于 5s 的危险值。
"""

from __future__ import annotations

import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from features.test_execution import runtime as te_runtime
from features.test_execution.tradefed import (
    DEFAULT_TRADEFED_STARTUP_TIMEOUT,
    MAX_TRADEFED_STARTUP_TIMEOUT,
    MIN_TRADEFED_STARTUP_TIMEOUT,
    execute_tradefed_command_local,
    tradefed_console_startup_timeout,
)


_SLOW_CONSOLE = textwrap.dedent("""\
    #!/bin/bash
    sleep 2
    printf 'cts-console > '
    while IFS= read -r line; do
      if [ "$line" = "exit" ]; then exit 0; fi
      printf 'Session  Pass  Fail  Result Directory\\n'
      printf 'cts-console > '
    done
""")

_SILENT_CONSOLE = textwrap.dedent("""\
    #!/bin/bash
    sleep 30
""")


def _make_suite(tmp: Path, body: str) -> tuple[str, str]:
    suite = tmp / "android-cts" / "tools"
    suite.mkdir(parents=True)
    launcher = suite / "cts-tradefed"
    launcher.write_text(body)
    launcher.chmod(0o755)
    return str(suite), str(launcher)


class TradefedStartupTimeoutTest(unittest.TestCase):
    def setUp(self):
        # execute_tradefed_command_local 只用 config_manager 计算 PATH 默认值。
        original = te_runtime.get_runtime().config_manager
        te_runtime.get_runtime().config_manager = SimpleNamespace(
            load_config=lambda: {},
            get_ubuntu_user=lambda config: "nobody",
        )
        self.addCleanup(
            setattr, te_runtime.get_runtime(), "config_manager", original
        )

    def test_env_override_clamp_and_fallback(self):
        with patch.dict(os.environ, {"GMS_TRADEFED_STARTUP_TIMEOUT": "90.5"}):
            self.assertEqual(tradefed_console_startup_timeout(), 90.5)
        # 低于最小安全预算的配置被钳制，防止误配置导致必然超时。
        with patch.dict(os.environ, {"GMS_TRADEFED_STARTUP_TIMEOUT": "0"}):
            self.assertEqual(
                tradefed_console_startup_timeout(), MIN_TRADEFED_STARTUP_TIMEOUT
            )
        with patch.dict(os.environ, {"GMS_TRADEFED_STARTUP_TIMEOUT": "not-a-number"}):
            self.assertEqual(
                tradefed_console_startup_timeout(), DEFAULT_TRADEFED_STARTUP_TIMEOUT
            )
        for invalid in ("nan", "inf", "-inf"):
            with self.subTest(invalid=invalid), patch.dict(
                os.environ, {"GMS_TRADEFED_STARTUP_TIMEOUT": invalid}
            ):
                self.assertEqual(
                    tradefed_console_startup_timeout(),
                    DEFAULT_TRADEFED_STARTUP_TIMEOUT,
                )
        with patch.dict(os.environ, {"GMS_TRADEFED_STARTUP_TIMEOUT": "999999"}):
            self.assertEqual(
                tradefed_console_startup_timeout(), MAX_TRADEFED_STARTUP_TIMEOUT
            )
        env = dict(os.environ)
        env.pop("GMS_TRADEFED_STARTUP_TIMEOUT", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                tradefed_console_startup_timeout(), DEFAULT_TRADEFED_STARTUP_TIMEOUT
            )

    def test_slow_startup_within_budget_succeeds(self):
        with TemporaryDirectory() as tmp:
            suite, launcher = _make_suite(Path(tmp), _SLOW_CONSOLE)
            env = dict(os.environ)
            env["GMS_TRADEFED_STARTUP_TIMEOUT"] = "20"
            with patch.dict(os.environ, env, clear=True):
                output, error, code = execute_tradefed_command_local(
                    suite, launcher
                )
        self.assertEqual(error, "")
        self.assertEqual(code, 0)
        self.assertIn("Session", output)

    def test_console_that_never_prompts_reports_budget_in_error(self):
        with TemporaryDirectory() as tmp:
            suite, launcher = _make_suite(Path(tmp), _SILENT_CONSOLE)
            env = dict(os.environ)
            env["GMS_TRADEFED_STARTUP_TIMEOUT"] = "5"
            with patch.dict(os.environ, env, clear=True):
                _output, error, _code = execute_tradefed_command_local(
                    suite, launcher
                )
        self.assertIn("Tradefed console startup timed out", error)
        self.assertIn("5s", error)


if __name__ == "__main__":
    unittest.main()
