"""Tradefed 命令辅助函数 - 查找、解析、执行 tradefed 命令"""

import contextlib
import logging
import os
import pty
import re
import select
import shlex
import subprocess
import time
from typing import Any
from pathlib import Path

from . import runtime


ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi_codes(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text or "")

logger = logging.getLogger(__name__)


def _android_build_tools_paths(platform_tools_path: str) -> list[str]:
    """Return Android build-tools directories for a non-login Tradefed process."""
    software_root = Path(platform_tools_path).parent
    candidates = []
    configured = os.environ.get("GMS_BUILD_TOOLS_PATH", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        software_root / "android-sdk-linux" / "build-tools",
        software_root.parent / "android-sdk" / "build-tools",
        Path("/usr/lib/android-sdk/build-tools"),
    ])
    paths: list[str] = []
    for root in candidates:
        if root.is_dir() and root.name == "build-tools":
            paths.extend(str(path) for path in sorted(root.glob("*"), reverse=True) if path.is_dir())
        elif root.is_dir():
            paths.append(str(root))
    return list(dict.fromkeys(paths))


def find_tradefed_binary(ssh, suite_path: str) -> str | None:
    """在指定目录中查找 tradefed 二进制文件"""
    find_cmd = f"find {shlex.quote(suite_path)} -maxdepth 1 -type f -executable -name '*-tradefed' 2>/dev/null | head -1"
    output, _, _ = runtime.ssh_manager.execute_command(ssh, find_cmd, timeout=10)
    result = output.strip()
    return result if result else None


def find_tradefed_binary_local(suite_path: str) -> str | None:
    """Find an executable tradefed launcher on the Controller filesystem."""
    directory = os.path.realpath(os.path.expanduser(suite_path))
    if not os.path.isdir(directory):
        return None
    for name in sorted(os.listdir(directory), key=str.lower):
        candidate = os.path.join(directory, name)
        if name.endswith("-tradefed") and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


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


# 表头可能粘连 tradefed 提示符（如 "cts-console > Session  Pass..."），需剥离前导提示符
_PROMPT_PREFIX_RE = re.compile(r"^[A-Za-z0-9_\-./() ]*>[ \t]*", re.IGNORECASE)


def _split_result_header(header_line: str) -> list[str]:
    """按 2+ 空格切分 tradefed 表头行，先剥离粘连的提示符前缀。"""
    line = _PROMPT_PREFIX_RE.sub("", header_line.replace("\r", "").strip(), count=1)
    return [seg.strip() for seg in re.split(r"\s{2,}", line) if seg.strip()]


def parse_tradefed_list_results(output: str) -> dict[str, Any]:
    """解析 tradefed list results 输出。

    不同套件列数不一致（CTS/GTS 多 Warning 列），以时间戳样式的
    Result Directory 作为锚点向两侧解析，不依赖固定列下标。
    """
    cleaned_output = strip_ansi_codes(output)

    results: list[dict[str, Any]] = []
    columns: list[str] = []
    lines = cleaned_output.split('\n')
    header_found = False
    _has_warning_col = False

    for line in lines:
        stripped = line.strip()

        if not header_found:
            if 'Session' in line and 'Pass' in line and 'Fail' in line:
                header_found = True
                columns = _split_result_header(line)
                _has_warning_col = any(c.lower() == 'warning' for c in columns)
            continue

        if not stripped or stripped.startswith('=====') or stripped.startswith('------'):
            continue
        # 跳过 tradefed 提示符回显行（如 "vts-tf >"、"cts-console >"）。
        if '>' in stripped and 'Session' not in stripped:
            continue

        parts = stripped.split()
        if len(parts) < 6:
            continue

        # 以时间戳样式的结果目录作为锚点，向两侧解析各列
        dir_index = next(
            (i for i, p in enumerate(parts) if _RESULT_DIR_RE.fullmatch(p)),
            None,
        )
        if dir_index is None:
            continue

        try:
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
            if _has_warning_col:
                try:
                    warning = str(int(parts[3]))
                except (ValueError, IndexError):
                    warning = ''

            # 锚点右侧：test_plan device_serial(s) build_id product
            tail = parts[dir_index + 1:]
            test_plan = tail[0] if len(tail) > 0 else ''
            build_id = ''
            product = ''
            device_tokens: list[str] = []
            if len(tail) >= 4:
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
    """通过 SSH 交互式 shell 执行 tradefed 命令。"""
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

    platform_tools_path = PLATFORM_TOOLS_PATH
    build_tools_path = ":".join([
        "$HOME/Software/android-sdk-linux/build-tools/19.0.0",
        "$HOME/android-sdk/build-tools/33.0.3",
    ])

    def wait_for_prompt(shell, prompt_patterns, timeout=10, poll_interval=0.05, require_stable=False):
        """轮询等待 shell 提示符出现。

        require_stable=True 时需输出连续多轮无变化才返回，适用于 tradefed
        控制台命令（输出异步到达，提示符可能先于结果出现）。
        list results 额外要求输出包含 Session 表头。
        """
        recv_timeout = min(poll_interval, 0.1)
        with contextlib.suppress(Exception):
            shell.settimeout(recv_timeout)

        output = ""
        start_time = time.time()
        last_output_time = start_time
        last_output_length = 0
        stable_count = 0
        stable_threshold = 3 if require_stable else 2
        needs_table_header = require_stable and command == "list results"

        def _check_done():
            """检测是否已到提示符且输出稳定。"""
            nonlocal stable_count, last_output_length
            i = output.rfind('\n')
            current_line = output[i + 1:] if i != -1 else output
            matched_prompt = any(re.search(pattern, current_line) for pattern in prompt_patterns)

            if matched_prompt and not require_stable:
                return True

            current_length = len(output)
            if current_length == last_output_length:
                stable_count += 1
                if matched_prompt and stable_count >= stable_threshold:
                    if needs_table_header and 'Session' not in output:
                        return False
                    return True
            else:
                stable_count = 0
                last_output_length = current_length
            return False

        while time.time() - start_time < timeout:
            received = False
            try:
                chunk = shell.recv(RECV_BUFFER_SIZE).decode('utf-8', errors='ignore')
                if chunk:
                    output += chunk
                    last_output_time = time.time()
                    received = True
                # recv 返回空串：连接暂无数据，仍参与稳定性计数。
            except Exception:
                # 超时无新数据时兜底返回
                if time.time() - last_output_time > STABLE_OUTPUT_TIMEOUT and output:
                    return output

            if not (received and require_stable) and _check_done():
                return output

            if not received:
                time.sleep(poll_interval)

        return output

    try:
        shell = ssh.invoke_shell()
        shell.settimeout(3)
        with contextlib.suppress(Exception):
            shell.recv(1024)

        shell.send(
            f"export PATH={shlex.quote(platform_tools_path)}:{build_tools_path}:$PATH\n"
        )
        wait_for_prompt(shell, [r'\$ ', r'\# ', '> '], timeout=2, poll_interval=0.05)

        shell.send(f"cd {shlex.quote(suite_path)}\n")
        wait_for_prompt(shell, [r'\$ ', r'\# ', '> '], timeout=2, poll_interval=0.05)

        shell.send(f"TERM=dumb {shlex.quote(tradefed_bin)}\n")
        tradefed_output = wait_for_prompt(shell, ['> ', 'tf> ', r'\(tf\)'], timeout=6, poll_interval=0.1)

        shell.send(f"{sanitize_tradefed_console_command(command)}\n")
        command_output = wait_for_prompt(shell, ['> ', 'tf> ', r'\(tf\)', 'All done'],
                                         timeout=30, poll_interval=0.1, require_stable=True)

        shell.send("exit\n")
        wait_for_prompt(shell, [r'\$ ', r'\# '], timeout=2, poll_interval=0.05)

        output = tradefed_output + command_output
        # 排空残余输出
        with contextlib.suppress(Exception):
            shell.settimeout(0.2)
            for _ in range(5):
                try:
                    chunk = shell.recv(16384).decode('utf-8', errors='ignore')
                except Exception:
                    break
                if not chunk:
                    break
                output += chunk
        with contextlib.suppress(Exception):
            shell.close()

        return output, "", 0

    except Exception as e:
        logger.error(f"[TRADEFED] Failed to execute command: {e}")
        return "", str(e), -1


def execute_tradefed_command_local(
    suite_path: str,
    tradefed_bin: str,
    command: str = "list results",
) -> tuple[str, str, int]:
    """Run a Controller-local tradefed console through stdin, without SSH."""
    safe_command = sanitize_tradefed_console_command(command)
    suite_directory = os.path.realpath(os.path.expanduser(suite_path))
    launcher = os.path.realpath(os.path.expanduser(tradefed_bin))
    if not os.path.isfile(launcher) or not os.access(launcher, os.X_OK):
        return "", f"Tradefed binary is not executable: {launcher}", -1
    if os.path.commonpath([launcher, suite_directory]) != suite_directory:
        return "", "Tradefed binary must stay inside the selected suite", -1

    config = runtime.config_manager.load_config()
    default_platform_tools = os.path.join(
        "/home",
        runtime.config_manager.get_ubuntu_user(config),
        "Software",
        "platform-tools",
    )
    env = os.environ.copy()
    env["TERM"] = "dumb"
    build_tools = _android_build_tools_paths(default_platform_tools)
    env["PATH"] = ":".join([
        *build_tools,
        os.environ.get("GMS_PLATFORM_TOOLS_PATH", default_platform_tools),
        env.get("PATH", ""),
    ])
    master_fd = -1
    process: subprocess.Popen | None = None
    output = ""

    def read_available(timeout: float) -> str:
        if master_fd < 0:
            return ""
        ready, _, _ = select.select([master_fd], [], [], timeout)
        if not ready:
            return ""
        try:
            return os.read(master_fd, 65536).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def prompt_visible(text: str) -> bool:
        plain = strip_ansi_codes(text).replace("\r", "")
        tail = plain.rsplit("\n", 1)[-1]
        return bool(re.search(r"(?:^|\s)(?:[\w.-]+(?:-tf|-console)?\s*)?>\s*$|\(tf\)\s*$", tail))

    try:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            [launcher],
            cwd=suite_directory,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)

        startup_deadline = time.monotonic() + 15
        while time.monotonic() < startup_deadline and process.poll() is None:
            output += read_available(0.1)
            if prompt_visible(output):
                break
        if process.poll() is not None and not prompt_visible(output):
            return output, "Tradefed exited before opening its console", process.returncode or -1
        if not prompt_visible(output):
            return output, "Tradefed console startup timed out", -1

        os.write(master_fd, f"{safe_command}\n".encode())
        command_started = len(output)
        command_deadline = time.monotonic() + 30
        last_data_at = time.monotonic()
        while time.monotonic() < command_deadline and process.poll() is None:
            chunk = read_available(0.1)
            if chunk:
                output += chunk
                last_data_at = time.monotonic()
                continue
            command_output = output[command_started:]
            if prompt_visible(command_output) and time.monotonic() - last_data_at >= 0.3:
                break
        else:
            return output, "Tradefed list results timed out", -1

        os.write(master_fd, b"exit\n")
        exit_deadline = time.monotonic() + 5
        while time.monotonic() < exit_deadline and process.poll() is None:
            output += read_available(0.1)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        output += read_available(0)
        return output, "", process.returncode or 0
    except (OSError, subprocess.SubprocessError) as exc:
        return output, str(exc), -1
    finally:
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()
                process.wait(timeout=2)
        if master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(master_fd)
