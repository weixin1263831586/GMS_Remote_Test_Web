"""Tradefed 命令辅助函数 - 查找、解析、执行 tradefed 命令"""

import contextlib
import logging
import os
import re
import time
from typing import Any

from . import runtime


ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi_codes(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text or "")

logger = logging.getLogger(__name__)


def find_tradefed_binary(ssh, suite_path: str) -> str | None:
    """在指定目录中查找 tradefed 二进制文件"""
    find_cmd = f"find '{suite_path}' -maxdepth 1 -type f -executable -name '*-tradefed' 2>/dev/null | head -1"
    output, _, _ = runtime.ssh_manager.execute_command(ssh, find_cmd, timeout=10)
    result = output.strip()
    return result if result else None


def parse_tradefed_list_results(output: str) -> list[dict[str, Any]]:
    """解析 tradefed list results 命令输出，支持 STS 和 VTS/CTS 两种格式"""
    cleaned_output = strip_ansi_codes(output)

    results = []
    lines = cleaned_output.strip().split('\n')
    header_found = False

    for line in lines:
        if not header_found:
            if 'Session' in line and 'Pass' in line and 'Fail' in line:
                header_found = True
            continue

        line = line.strip()
        if not line or line.startswith('=====') or line.startswith('------'):
            continue

        if '>' in line and 'Session' not in line:
            continue

        parts = line.split()
        if len(parts) >= 10:
            try:
                has_of_keyword = len(parts) > 4 and parts[4] == 'of'

                if has_of_keyword:
                    result_entry = {
                        'session': parts[0],
                        'pass': int(parts[1]),
                        'fail': int(parts[2]),
                        'modules': parts[3],
                        'modules_total': parts[5],
                        'result_directory': parts[6],
                        'test_plan': parts[7],
                        'device_serial': parts[8],
                        'build_id': parts[9],
                        'product': parts[10] if len(parts) > 10 else ''
                    }
                else:
                    result_entry = {
                        'session': parts[0],
                        'pass': int(parts[1]),
                        'fail': int(parts[2]),
                        'modules': parts[3],
                        'modules_total': parts[4],
                        'result_directory': parts[5],
                        'test_plan': parts[6],
                        'device_serial': parts[7],
                        'build_id': parts[8],
                        'product': parts[9] if len(parts) > 9 else ''
                    }
                results.append(result_entry)
            except (ValueError, IndexError):
                continue

    return results


def execute_tradefed_command(ssh, suite_path: str, tradefed_bin: str, command: str = "list results") -> tuple:
    """
    执行 tradefed 命令（使用登录 shell 加载环境变量）

    使用 invoke_shell 交互式方式执行命令，适用于所有测试套件类型

    性能优化：使用智能等待替代固定延迟，大幅减少查询时间
    """
    # 常量定义
    config = runtime.config_manager.load_config()
    default_platform_tools = os.path.join(
        "/home",
        runtime.config_manager.get_ubuntu_user(config),
        "Software",
        "platform-tools"
    )
    PLATFORM_TOOLS_PATH = os.environ.get("GMS_PLATFORM_TOOLS_PATH", default_platform_tools)
    RECV_BUFFER_SIZE = 8192
    STABLE_OUTPUT_TIMEOUT = 2.0
    POLL_INTERVAL = 0.05

    platform_tools_path = PLATFORM_TOOLS_PATH

    def wait_for_prompt(shell, prompt_patterns, timeout=10, poll_interval=0.05):
        """智能等待 shell 提示符出现（优化版）"""
        output = ""
        start_time = time.time()
        last_output_time = start_time
        last_output_length = 0
        stable_count = 0

        while time.time() - start_time < timeout:
            try:
                chunk = shell.recv(RECV_BUFFER_SIZE).decode('utf-8', errors='ignore')
                if chunk:
                    output += chunk
                    last_output_time = time.time()

                    for pattern in prompt_patterns:
                        current_line = output.split('\n')[-1:][0] if output.split('\n') else ''
                        if re.search(pattern, current_line):
                            return output

                    current_length = len(output)
                    if current_length == last_output_length:
                        stable_count += 1
                        if stable_count >= 3:
                            return output
                    else:
                        stable_count = 0
                        last_output_length = current_length
            except Exception:
                if time.time() - last_output_time > STABLE_OUTPUT_TIMEOUT:
                    return output
            time.sleep(POLL_INTERVAL)

        return output

    # 使用 invoke_shell 交互式执行
    try:
        shell = ssh.invoke_shell()
        shell.settimeout(3)

        with contextlib.suppress(Exception):
            shell.recv(1024)

        shell.send(f"export PATH={platform_tools_path}:$PATH\n")
        wait_for_prompt(shell, [r'\$ ', r'\# ', '> '], timeout=2, poll_interval=0.05)

        shell.send(f"cd {suite_path}\n")
        wait_for_prompt(shell, [r'\$ ', r'\# ', '> '], timeout=2, poll_interval=0.05)

        shell.send(f"TERM=dumb {tradefed_bin}\n")
        tradefed_output = wait_for_prompt(shell, ['> ', 'tf> ', r'\(tf\)'], timeout=6, poll_interval=0.1)

        shell.send(f"{command}\n")
        command_output = wait_for_prompt(shell, ['> ', 'tf> ', r'\(tf\)', 'All done'],
                                         timeout=20, poll_interval=0.1)

        time.sleep(0.5)

        shell.send("exit\n")
        wait_for_prompt(shell, [r'\$ ', r'\# '], timeout=2, poll_interval=0.05)

        output = tradefed_output + command_output
        max_retries = 10
        for _ in range(max_retries):
            try:
                chunk = shell.recv(16384).decode('utf-8', errors='ignore')
                if not chunk:
                    break
                output += chunk
                time.sleep(0.1)
            except Exception:
                break

        with contextlib.suppress(Exception):
            shell.close()

        return output, "", 0

    except Exception as e:
        logger.error(f"[TRADEFED] Failed to execute command: {e}")
        return "", str(e), -1
