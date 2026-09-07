"""Source-side firmware flash dispatcher for USB/IP-owned devices.

15.txt 架构结论（实机修正版）：USB/IP detach 后设备回到 Windows 源主机，
不会出现在 Controller 本机 ADB——因此烧写必须下发到 Windows 源端执行。

Windows 端无 CLI 烧写工具（RKDevTool 仅 GUI），且 SSH 会话启动的 GUI
进程没有可见窗口（session 隔离，实测 MainWindowHandle=0），无法从
Controller 直接自动化。因此采用「文件队列式 Source Agent」：

    Controller (Linux)                    Windows 源主机
    ---------------                       --------------
    1. SFTP 上传固件到        ------->    scripts/windows_source_agent.py
       C:\\gms-flash\\<task>\\            （桌面会话常驻，计划任务 /IT
    2. 投递 <task>.json 任务               自启动，可操作 RKDevTool GUI）
    3. 轮询 <task>.result.json  <------    RKDevTool GUI 自动化烧写
    4. 校验结果                           轮询自身按天滚动日志判定成败

超时/失败不降级重试，由上层决定设备是否进入隔离状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from foundation.ssh_security import configure_strict_host_keys

from . import runtime


logger = logging.getLogger(__name__)

# 队列目录跟随 SSH 登录用户（%USERPROFILE%\gms-flash-queue），
# 与 Agent 端 Path.home() 推导一致。
WINDOWS_FIRMWARE_DIR = r"C:\gms-flash"
RESULT_POLL_INTERVAL_SECONDS = 10.0
RESULT_TIMEOUT_SECONDS = 5400
# device 串号会拼进 task_id 并被 wait_result 的 `type "..."` cmd.exe
# 命令插值：只放行无 cmd 元字符的字符集，杜绝引号/>& 逃逸。
_SOURCE_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def windows_queue_dir(device_host: str) -> str:
    """Return the Windows flash queue dir for the SSH login user.

    Agent 用 Path.home()/gms-flash-queue（%USERPROFILE%），SSH 登录用户
    与运行 Agent 的桌面账户一致时，等价于 C:\\Users\\<user>\\gms-flash-queue。
    """
    username = str(device_host or "").split("@", 1)[0].strip()
    if not username:
        return r"C:\Users\hcq\gms-flash-queue"
    return rf"C:\Users\{username}\gms-flash-queue"


class SourceFlashError(RuntimeError):
    """Raised when a source-side flash step fails."""

    def __init__(self, message: str, *, status_code: int = 500,
                 stage: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.stage = stage


@dataclass
class SourceFlashReport:
    device: str
    success: bool
    stage: str
    status: str = ""
    log_tail: str = ""
    error: str = ""
    elapsed_seconds: float = 0.0


def open_windows_ssh(device_host: str):
    """Open an SSH session to the Windows source host using stored creds."""
    host = str(device_host or "").strip()
    username, hostname = host.split("@", 1) if "@" in host else ("", host)
    config = runtime.config_manager.load_config()
    # 凭据查找走 config_manager（foundation 层，避免跨 feature import）。
    password = (
        runtime.config_manager.find_device_host_password(host)
        or config.get("device_pswd", "")
    )
    if not password:
        raise SourceFlashError(
            f"未找到 Windows 源主机 {host} 的 SSH 凭据",
            status_code=409, stage="SSH",
        )
    import paramiko

    client = paramiko.SSHClient()
    # 固件烧写是高风险链路，必须走严格主机密钥校验（known_hosts + Reject）。
    configure_strict_host_keys(client)
    try:
        client.connect(
            hostname, username=username, password=password, timeout=15,
        )
    except Exception as exc:
        raise SourceFlashError(
            f"SSH 连接 Windows 源主机 {host} 失败: {exc}",
            status_code=502, stage="SSH",
        ) from exc
    return client


def windows_exec(ssh, command: str, timeout: int = 30) -> tuple[str, int]:
    _stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    return (out + ("\n" + err if err else "")).strip(), code


def sftp_mkdir_chain(sftp, remote_dir: str) -> None:
    probe = ""
    for part in remote_dir.split("\\"):
        if not part:
            probe += "\\"
            continue
        probe = probe + part + "\\"
        try:
            sftp.stat(probe)
        except FileNotFoundError:
            sftp.mkdir(probe)


def sftp_upload(ssh, local_path: str, remote_path: str,
                remote_dir: str) -> None:
    sftp = ssh.open_sftp()
    try:
        sftp_mkdir_chain(sftp, remote_dir)
        sftp.put(local_path, remote_path)
    finally:
        sftp.close()


def enqueue_task(ssh, queue_dir: str, task_id: str, firmware: str,
                 device: str = "") -> None:
    sftp = ssh.open_sftp()
    try:
        try:
            sftp.stat(queue_dir)
        except FileNotFoundError:
            sftp.mkdir(queue_dir)
        # 任务必须携带目标设备：Agent 端用 adb -s <device> reboot loader，
        # 多设备在线时才能烧对目标。
        task_spec = {"firmware": firmware, "device": device}
        with sftp.open(f"{queue_dir}\\{task_id}.json", "w") as f:
            f.write(json.dumps(task_spec, ensure_ascii=False))
    finally:
        sftp.close()


def wait_result(ssh, queue_dir: str, task_id: str, keepalive=None) -> dict:
    result_path = f"{queue_dir}\\{task_id}.result.json"
    deadline = time.time() + RESULT_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(RESULT_POLL_INTERVAL_SECONDS)
        if keepalive is not None:
            with contextlib.suppress(Exception):
                keepalive()
        out, code = windows_exec(
            ssh, f'type "{result_path}" 2>nul', timeout=15,
        )
        if code == 0 and out.strip():
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                logger.warning(
                    "Source Agent result not valid JSON yet: %s", out[:120],
                )
                continue
    raise SourceFlashError(
        f"Source Agent 烧写超时（{RESULT_TIMEOUT_SECONDS}s）",
        stage="FLASHING",
    )


def _sync_flash_flow(
    *,
    device: str,
    device_host: str,
    firmware_path: str,
    loop: asyncio.AbstractEventLoop,
    on_log,
    keepalive=None,
) -> SourceFlashReport:
    started = time.time()

    if not _SOURCE_DEVICE_ID_RE.fullmatch(device or ""):
        raise SourceFlashError(
            f"invalid source device id: {device!r}", stage="FLASHING")

    def log(message: str) -> None:
        if on_log is not None:
            asyncio.run_coroutine_threadsafe(on_log(message), loop)

    ssh = open_windows_ssh(device_host)
    try:
        task_id = f"flash-{device}-{int(time.time())}"
        remote_dir = f"{WINDOWS_FIRMWARE_DIR}\\{task_id}"
        remote_firmware = f"{remote_dir}\\{os.path.basename(firmware_path)}"
        queue_dir = windows_queue_dir(device_host)

        log(f"上传固件到 Windows 源主机: {remote_firmware}")
        sftp_upload(ssh, firmware_path, remote_firmware, remote_dir)

        log(f"投递烧写任务 {task_id} 到 Source Agent（目标设备 {device}）")
        enqueue_task(ssh, queue_dir, task_id, remote_firmware, device=device)

        result = wait_result(ssh, queue_dir, task_id, keepalive=keepalive)
        report = SourceFlashReport(
            device=device,
            success=result.get("status") == "SUCCESS",
            stage="SUCCEEDED" if result.get("status") == "SUCCESS"
            else "FLASHING",
            status=result.get("status", ""),
            log_tail=result.get("log_tail", "")[-1500:],
            error=result.get("error", ""),
            elapsed_seconds=time.time() - started,
        )
        if not report.success:
            raise SourceFlashError(
                f"Source Agent 烧写失败（{report.status}）: "
                f"{report.error or report.log_tail[-300:]}",
                stage="FLASHING",
            )
        log("源端烧写完成，等待 Controller 重新导出 USB/IP")
        return report
    finally:
        try:
            ssh.close()
        except Exception:
            pass


async def run_source_flash(
    *,
    device: str,
    device_host: str,
    firmware_path: str,
    on_log=None,
    keepalive=None,
) -> SourceFlashReport:
    """Dispatch a complete-firmware flash to the Windows source agent.

    ``device_host`` is the Windows source host (e.g. hcq@172.16.14.66).
    ``firmware_path`` is the firmware already present on the Controller
    (Linux) filesystem; it is SFTP-uploaded to the source host first.
    ``keepalive`` is an optional no-argument callback invoked during the
    result polling loop so callers can refresh long-held locks.
    """
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(
        _sync_flash_flow,
        device=device,
        device_host=device_host,
        firmware_path=firmware_path,
        loop=loop,
        on_log=on_log,
        keepalive=keepalive,
    )
