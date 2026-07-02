"""Tradefed 命令辅助函数 - 查找、解析、执行 tradefed 命令"""

import contextlib
import logging
import os
import re
import shlex
import time
from typing import Any

from . import runtime


ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi_codes(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text or "")

logger = logging.getLogger(__name__)


def find_tradefed_binary(ssh, suite_path: str) -> str | None:
    """在指定目录中查找 tradefed 二进制文件"""
    find_cmd = f"find {shlex.quote(suite_path)} -maxdepth 1 -type f -executable -name '*-tradefed' 2>/dev/null | head -1"
    output, _, _ = runtime.ssh_manager.execute_command(ssh, find_cmd, timeout=10)
    result = output.strip()
    return result if result else None


def sanitize_tradefed_console_command(command: str) -> str:
    """Reject command text that could escape the tradefed console interaction."""
    command = str(command or '').strip()
    if not command:
        return 'list results'
    if any(char in command for char in '\r\n\x00'):
        raise ValueError('tradefed command must be a single line')
    if len(command) > 200:
        raise ValueError('tradefed command is too long')
    return command


_RESULT_DIR_RE = re.compile(r"\b\d{4}\.\d{2}\.\d{2}(?:[_\d.]+)?\b")


def _split_result_header(header_line: str) -> list[str]:
    """把 tradefed 原始表头行拆成列名列表，保留原始列名。

    tradefed 表头形如:
        "Session  Pass  Fail  Warning  Modules Complete  Result Directory  ..."
    按两个以上空格切分即可得到各列；"Modules Complete"、"Result Directory"、
    "Device serial(s)"、"Build ID"、"Test Plan" 各自是单列（含内部空格）。
    """
    # 去掉回车与首尾空白后，按 2+ 空格分段。
    return [seg.strip() for seg in re.split(r"\s{2,}", header_line.replace("\r", "").strip()) if seg.strip()]


def parse_tradefed_list_results(output: str) -> dict[str, Any]:
    """解析 tradefed ``list results`` 命令输出。

    不同套件输出列不一致：CTS/GTS 含 ``Warning`` 列、且 ``Modules Complete``
    形如 ``1 of 1``；VTS/STS 无 ``Warning`` 列；设备序列号可能由多个以空格
    分隔的 token 组成（如 ``RK3576GMS2, RK357603``）。因此不依赖固定列下标，
    而是以表头检测 ``Warning`` 列、并以时间戳样式的 ``Result Directory``
    作为锚点向两侧解析。
    """
    cleaned_output = strip_ansi_codes(output)

    results: list[dict[str, Any]] = []
    columns: list[str] = []
    lines = cleaned_output.split('\n')
    header_found = False

    for line in lines:
        stripped = line.strip()

        if not header_found:
            if 'Session' in line and 'Pass' in line and 'Fail' in line:
                header_found = True
                columns = _split_result_header(line)
            continue

        if not stripped or stripped.startswith('=====') or stripped.startswith('------'):
            continue
        # 跳过 tradefed 提示符回显行（如 "vts-tf >"、"cts-console >"）。
        if '>' in stripped and 'Session' not in stripped:
            continue

        parts = stripped.split()
        if len(parts) < 6:
            continue

        # 以时间戳样式的结果目录作为稳定锚点（兼容 2026.06.25_10.57.05 与
        # 2026.07.01_17.02.29.859_2402 两种格式）。
        dir_index = next(
            (i for i, p in enumerate(parts) if _RESULT_DIR_RE.fullmatch(p)),
            None,
        )
        if dir_index is None:
            continue

        try:
            # 锚点左侧：session pass fail [warning] modules [of] modules_total
            session = parts[0]
            pass_count = int(parts[1])
            fail_count = int(parts[2])
            modules = ''
            modules_total = ''
            warning = ''
            if dir_index >= 4 and parts[dir_index - 2] == 'of':
                modules = parts[dir_index - 3]
                modules_total = parts[dir_index - 1]
            elif dir_index >= 3:
                modules = parts[dir_index - 1]
            # CTS/GTS 在 fail 与 modules 之间多一列 Warning。
            if 'warning' in str(columns).lower():
                try:
                    warning = str(int(parts[3]))
                except (ValueError, IndexError):
                    warning = ''

            # 锚点右侧：test_plan device_serial(s)... build_id product
            tail = parts[dir_index + 1:]
            test_plan = tail[0] if len(tail) > 0 else ''
            build_id = ''
            product = ''
            device_tokens: list[str] = []
            if len(tail) >= 4:
                # product 是最后一个 token；build_id 是倒数第二个。
                product = tail[-1]
                build_id = tail[-2]
                device_tokens = tail[1:-2]
            elif len(tail) == 3:
                build_id = tail[-1]
                device_tokens = tail[1:-1]
            elif len(tail) == 2:
                device_tokens = tail[1:]

            result_entry = {
                'session': session,
                'pass': pass_count,
                'fail': fail_count,
                'warning': warning,
                'modules': modules,
                'modules_total': modules_total,
                'result_directory': parts[dir_index],
                'test_plan': test_plan,
                'device_serial': ' '.join(device_tokens),
                'build_id': build_id,
                'product': product,
            }
            results.append(result_entry)
        except (ValueError, IndexError):
            continue

    return {'columns': columns, 'results': results}



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

    def wait_for_prompt(shell, prompt_patterns, timeout=10, poll_interval=0.05, require_stable=False):
        """智能等待 shell 提示符出现（优化版）

        ``require_stable`` 适用于 tradefed 控制台命令（如 ``list results``）：
        这类命令的输出会异步产生，且 tradefed 可能在结果真正打印前就再次
        回显提示符（例如 ``vts-tf >``）。此时仅在"看到提示符"就返回会漏掉
        结果表。开启 ``require_stable`` 后，必须满足"最后一行是提示符 且
        输出长度已连续多次未变化"才返回，从而等到延迟到达的结果输出。
        """
        output = ""
        start_time = time.time()
        last_output_time = start_time
        last_output_length = 0
        stable_count = 0
        prompt_seen = False

        while time.time() - start_time < timeout:
            try:
                chunk = shell.recv(RECV_BUFFER_SIZE).decode('utf-8', errors='ignore')
                if chunk:
                    output += chunk
                    last_output_time = time.time()
                else:
                    # recv 返回空：连接暂无数据，仍参与稳定性计数。
                    pass

                current_line = output.split('\n')[-1:][0] if output.split('\n') else ''
                matched_prompt = any(re.search(pattern, current_line) for pattern in prompt_patterns)

                if matched_prompt and not require_stable:
                    return output

                current_length = len(output)
                if current_length == last_output_length:
                    stable_count += 1
                    if require_stable:
                        # 等到提示符出现且输出稳定，避免漏掉异步延迟的结果表。
                        if matched_prompt and stable_count >= 5:
                            return output
                    elif stable_count >= 3:
                        return output
                else:
                    stable_count = 0
                    last_output_length = current_length

                if matched_prompt:
                    prompt_seen = True
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

        # shlex.quote every caller-supplied token before it reaches the login
        # shell — suite_path / tradefed_bin come from the client (WebSocket /
        # API) and were previously interpolated raw, allowing command injection.
        shell.send(f"export PATH={shlex.quote(platform_tools_path)}:$PATH\n")
        wait_for_prompt(shell, [r'\$ ', r'\# ', '> '], timeout=2, poll_interval=0.05)

        shell.send(f"cd {shlex.quote(suite_path)}\n")
        wait_for_prompt(shell, [r'\$ ', r'\# ', '> '], timeout=2, poll_interval=0.05)

        shell.send(f"TERM=dumb {shlex.quote(tradefed_bin)}\n")
        tradefed_output = wait_for_prompt(shell, ['> ', 'tf> ', r'\(tf\)'], timeout=6, poll_interval=0.1)

        shell.send(f"{sanitize_tradefed_console_command(command)}\n")
        command_output = wait_for_prompt(shell, ['> ', 'tf> ', r'\(tf\)', 'All done'],
                                         timeout=30, poll_interval=0.1, require_stable=True)

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
