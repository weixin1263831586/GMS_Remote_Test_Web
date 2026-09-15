"""gms-rt-system-update 的 TLS 透传回归（从 test_skill_cli.py 拆出）。

fixture 钉 XDG_*/GMS_REMOTE_TEST_SERVER 进沙箱：CI 无本机 Controller，
helper 的本地 5001 探活启发式不得影响更新生命周期测试。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-remote-test.sh"


class SkillUpdateEnvTests(unittest.TestCase):
    """gms-rt-system-update 必须走 gms-agent update 并传导当前 TLS 配置。

    install.sh 已随包结构迁移删除，更新生命周期改为
    `gms-agent update`（registry → 校验 → versions/<v>/ → 整包重激活）。
    命令现在优先取本脚本旁边的 gms-agent，并把会话的
    GMS_INSTALL_CA_CERT / GMS_INSTALL_INSECURE 传导给 gms-agent 的下载层。
    """

    def test_update_invokes_gms_agent_with_tls_passthrough(self):
        with tempfile.TemporaryDirectory() as temporary:
            scripts_dir = Path(temporary) / "scripts"
            scripts_dir.mkdir()
            helper_copy = scripts_dir / "gms-remote-test.sh"
            helper_copy.write_bytes(HELPER.read_bytes())
            # Stub gms-agent（与真实入口一致：#!/usr/bin/env python3）：
            # 记录收到的关键环境变量后成功退出。
            env_dump = Path(temporary) / "agent-env.json"
            (scripts_dir / "gms-agent").write_text(
                "#!/usr/bin/env python3\n"
                'import os, sys\n'
                'ca = os.environ.get("GMS_INSTALL_CA_CERT", "")\n'
                'insecure = os.environ.get("GMS_INSTALL_INSECURE", "")\n'
                'with open(r"' + str(env_dump) + '", "w") as fh:\n'
                '    fh.write("\\n".join([ca, insecure, *sys.argv[1:]]) + "\\n")\n',
                encoding="utf-8",
            )
            (scripts_dir / "gms-agent").chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "HOME": temporary,
                    # 无 profile 时 helper 会回退到「本机 5001 是否有服务监听」
                    # 的本地启发式；显式给服务器，测试不再依赖本机是否恰好
                    # 跑着 Controller（CI 上没有）。
                    "GMS_REMOTE_TEST_SERVER": "https://controller.example:5001",
                    "GMS_CURL_CA_CERT": "/tmp/trusted-ca.pem",
                    "GMS_CURL_INSECURE": "",
                    "XDG_CONFIG_HOME": str(Path(temporary) / ".config"),
                    "XDG_DATA_HOME": str(Path(temporary) / ".local" / "share"),
                    "XDG_STATE_HOME": str(Path(temporary) / ".local" / "state"),
                    "NO_COLOR": "1",
                }
            )
            result = subprocess.run(
                ["bash", str(helper_copy), "gms-rt-system-update"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = env_dump.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        ca_cert, insecure, *argv = lines[:3]
        # 当前会话的 TLS 配置必须传导给 gms-agent（CA 优先，未配置时回退 0）。
        self.assertEqual(ca_cert, "/tmp/trusted-ca.pem")
        self.assertEqual(insecure, "0")
        # 新生命周期：python3 <scripts>/gms-agent update
        self.assertTrue(any(arg == "update" for arg in argv), lines[3:])


if __name__ == "__main__":
    unittest.main()
