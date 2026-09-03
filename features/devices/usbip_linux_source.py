"""Ubuntu/Linux USB/IP 来源主机支持。

来源侧使用 ``tools/usbipd``（用户态 USB/IP 服务端，fork 自
jiegec/usbip）导出 Android 设备；接入侧（Controller/Worker）继续使用
内核 ``vhci_hcd`` + ``usbip attach``，与 Windows 来源完全一致。

与 usbipd-win 的差异：
- 无 ``bind/detach/unbind`` 按设备命令：一个 ``usbipd bind`` 进程按
  ``--serial/--vid`` 过滤器导出全部匹配设备，"绑定"即保持进程存活；
- 无 Shared/Attached 状态表：设备清单直接来自 sysfs/udev；
- BUSID 由服务端按 ``busnum-address-port`` 派生，可用 udev 属性预测。

来源缺少可用 usbipd（未安装或版本过低）时，``ensure_ubuntu_usbip_server``
会先自动部署随平台分发的 ``tools/usbipd``：优先 sudo 安装到
``/usr/local/bin``，无免密 sudo 的主机回退安装到 ``~/.local/bin``。

生命周期与安全约定：
- 启动时把 PID 写入 ``~/.local/state/gms-usbipd.pid``；查询/停止优先按
  PID + ``/proc/<pid>/cmdline`` 归属校验，PID 文件不可用时才回退到
  ``pgrep/pkill -f``（并严格匹配 ``usbipd bind`` argv 前缀）；
- 所有进入 shell 的串号/VID/二进制路径一律经 ``shlex.quote``，
  USB serial 是外部设备输入，禁止直接拼接。
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

LINUX_USBIPD_BIN = "/usr/local/bin/usbipd"
LINUX_USBIPD_MIN_VERSION = (0, 9, 2)  # 支持可重复 --serial/--vid
LINUX_USBIPD_LOG = "/tmp/usbipd-gms.log"
LINUX_USBIPD_UPLOAD = "/tmp/usbipd-gms-upload"
LINUX_USBIPD_BIND_PATTERN = "usbipd bind"
LINUX_USBIPD_PID_FILE = "$HOME/.local/state/gms-usbipd.pid"

LINUX_USBIPD_INSTALL_GUIDE = (
    "Ubuntu来源主机需要用户态usbipd服务端(v0.9.2+)。可由平台自动上传"
    "（仓库 tools/usbipd），或手动执行：\n"
    "scp tools/usbipd USER@HOST:/tmp/usbipd\n"
    "ssh USER@HOST \"sudo install -m 0755 /tmp/usbipd /usr/local/bin/usbipd\"\n"
    "验证安装：usbipd --version"
)

# udevadm info --query=property 输出行，例如 ``ID_VENDOR_ID=2207``。
_UDEV_PROPERTY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
# ``usbip list -r`` 导出行，例如 ``1-17-13: Fuzhou Rockchip ... (2207:0006)``。
_USBIP_EXPORT_LINE_RE = re.compile(r"^\s+(\d+(?:-\d+)+):\s+(.*)$")
_VID_PID_TEXT_RE = re.compile(r"\(([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\)")


def linux_usbipd_not_installed_error() -> dict[str, Any]:
    """Standard error payload used when the Ubuntu source lacks usbipd."""
    return {
        "success": False,
        "error": "usbipd未安装",
        "install_guide": LINUX_USBIPD_INSTALL_GUIDE,
    }


def source_os_label(source_os: str) -> str:
    """Public label for a source host OS."""
    return {"windows": "Windows", "linux": "Ubuntu"}.get(
        str(source_os or "").strip(), "未知"
    )


def parse_udev_property_blocks(output: str) -> list[dict[str, str]]:
    """Parse ``udevadm info`` property stanzas (one block per USB device)."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _UDEV_PROPERTY_RE.match(stripped)
        if match:
            if current is None:
                current = {}
            current[match.group(1)] = match.group(2).strip()
            continue
        # 任意非属性行（如 @@DEV 分隔）结束上一个块。
        if current is not None:
            blocks.append(current)
            current = None
    if current is not None:
        blocks.append(current)
    return blocks


def predict_linux_usbip_busid(busnum: str | int, devnum: str | int, devpath: str) -> str:
    """Predict the busid the userspace usbipd assigns to a sysfs device.

    服务端 BUSID 为 ``busnum-address-port``（rusb 的 bus_number、address、
    port_number）；port_number 是设备在父集线器上的端口，即 sysfs DEVPATH
    末段（``1-13.4`` → 4，``1-13`` → 13）。
    """
    try:
        bus = int(str(busnum).strip() or "0")
        address = int(str(devnum).strip() or "0")
    except ValueError:
        return ""
    segments = [part for part in str(devpath or "").strip().split("/") if part]
    leaf = segments[-1] if segments else ""
    port_text = leaf.split(".")[-1] if leaf else ""
    if "-" in port_text:
        # 顶层设备（如 ``1-3``）没有 "." 层级，端口是 "-" 后的数字。
        port_text = port_text.rsplit("-", 1)[-1]
    try:
        port = int(port_text)
    except ValueError:
        return ""
    if bus <= 0 or address <= 0 or port <= 0:
        return ""
    return f"{bus}-{address}-{port}"


def parse_usbip_running_cmdline(cmdline: str) -> dict[str, Any]:
    """Extract pid/serial/vid filters from a running ``usbipd bind`` command line.

    ``cmdline`` 形如 ``1234 /usr/local/bin/usbipd bind --serial S1``；
    仅当第一个非 PID 的 argv 是 ``usbipd``（二进制名精确匹配）且第二个
    argv 是 ``bind`` 时才算运行中，避免把恰好包含该字符串的其他进程
    （如编辑器、grep）误判为 usbipd。
    """
    text = str(cmdline or "").strip()
    tokens = text.split()
    pid = ""
    if tokens and tokens[0].isdigit():
        pid = tokens[0]
        tokens = tokens[1:]
    binary_name = Path(tokens[0]).name if tokens else ""
    running = binary_name == "usbipd" and len(tokens) >= 2 and tokens[1] == "bind"
    serials = re.findall(r"--serial[= ](\S+)", text)
    vids = re.findall(r"--vid[= ]([0-9A-Fa-f]+)", text)
    return {
        "running": running,
        "pid": pid if running else "",
        "cmdline": text,
        "serials": serials,
        "vids": [value.lower() for value in vids],
    }


def _read_usbipd_pid_file(ssh_manager, ssh) -> str:
    stdout, _stderr, code = ssh_manager.execute_command(
        ssh, f"cat {LINUX_USBIPD_PID_FILE} 2>/dev/null", timeout=5,
    )
    if code != 0:
        return ""
    pid = (stdout or "").strip().splitlines()
    return pid[0].strip() if pid and pid[0].strip().isdigit() else ""


def _proc_pid_matches(ssh_manager, ssh, pid: str) -> bool:
    """Verify /proc/<pid> exists and its argv matches ``usbipd bind``."""
    stdout, _stderr, code = ssh_manager.execute_command(
        ssh,
        f"[ -d /proc/{pid} ] && tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null",
        timeout=5,
    )
    if code != 0:
        return False
    argv = (stdout or "").strip()
    # argv0 末段必须是 usbipd，且 argv 包含 bind 子命令。
    argv0 = argv.split(None, 1)
    return bool(argv0) and (
        argv0[0].endswith("/usbipd") or argv0[0] == "usbipd"
    ) and " bind" in f" {argv} "


def query_ubuntu_running_usbipd(ssh_manager, ssh) -> dict[str, Any]:
    """Inspect a running ``usbipd bind`` process on the Ubuntu source.

    优先读取 PID 文件并用 /proc/<pid>/cmdline 校验归属；PID 文件缺失或
    指向的进程已退出时，回退到严格匹配 argv 的 ``pgrep -af``。
    """
    pid = _read_usbipd_pid_file(ssh_manager, ssh)
    if pid:
        stdout, _stderr, _code = ssh_manager.execute_command(
            ssh,
            f"tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null",
            timeout=5,
        )
        cmdline = (stdout or "").strip()
        info = parse_usbip_running_cmdline(f"{pid} {cmdline}".strip())
        if info["running"]:
            return info
    stdout, _stderr, code = ssh_manager.execute_command(
        ssh, f"pgrep -af {shlex.quote(LINUX_USBIPD_BIND_PATTERN)}", timeout=10,
    )
    cmdline = ""
    if code == 0:
        lines = [
            line.strip() for line in (stdout or "").splitlines()
            if parse_usbip_running_cmdline(line.strip())["running"]
        ]
        if lines:
            cmdline = lines[0]
    return parse_usbip_running_cmdline(cmdline)


def _stop_linux_usbipd(ssh_manager, ssh, signal_flag: str = "") -> bool:
    """Stop the owned usbipd process; PID-file ownership first, pkill fallback.

    返回 True 表示目标进程已不存在（从未运行或已停止）。
    """
    pid = _read_usbipd_pid_file(ssh_manager, ssh)
    if pid and _proc_pid_matches(ssh_manager, ssh, pid):
        # 精确归属：只杀 PID 文件记录且 argv 校验通过的那个实例。
        stdout, _stderr, _code = ssh_manager.execute_command(
            ssh, f"kill {signal_flag} {pid} 2>/dev/null; true", timeout=10,
        )
        return not (stdout or "").strip()
    # 回退：严格匹配 argv（限定二进制名 usbipd + bind 子命令），
    # 避免 pkill -f 误杀命令行中恰好包含该字符串的无关进程。
    pattern = "[u]sbipd bind"
    stdout, _stderr, _code = ssh_manager.execute_command(
        ssh, f"pkill {signal_flag} -f {shlex.quote(pattern)}; true", timeout=10,
    )
    return not (stdout or "").strip()


def _device_matches_android(
    properties: dict[str, str],
    vid_pids: tuple[str, ...] = (),
    markers: tuple[str, ...] = (),
) -> bool:
    vendor = (properties.get("ID_VENDOR_ID") or "").lower()
    model = (properties.get("ID_MODEL_ID") or "").lower()
    vid_pid = f"{vendor}:{model}" if vendor and model else ""
    if vid_pid and vid_pid in {
        str(item or "").lower() for item in vid_pids or ()
    }:
        return True
    if vendor and vendor in {
        str(item or "").split(":", 1)[0]
        for item in vid_pids or ()
        if item
    }:
        return True
    text = " ".join((
        properties.get("ID_VENDOR") or "",
        properties.get("ID_VENDOR_FROM_DATABASE") or "",
        properties.get("ID_MODEL") or "",
        properties.get("ID_MODEL_FROM_DATABASE") or "",
    )).lower()
    return any(marker in text for marker in markers or ())


def list_ubuntu_usb_devices(
    ssh_manager,
    ssh,
    vid_pids: tuple[str, ...] = (),
    markers: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Enumerate USB devices on an Ubuntu source via udev properties.

    vid_pids/markers 为空时返回全部 USB 设备（供 allow_transport_only 的
    BUSID 存在性检查使用）。
    """
    command = (
        "for d in /dev/bus/usb/*/*; do "
        'echo "@@DEV $d"; '
        'udevadm info --query=property --name="$d" 2>/dev/null; '
        "echo @@END; done"
    )
    stdout, stderr, code = ssh_manager.execute_command(ssh, command, timeout=25)
    output = stdout or ""
    if code != 0 and not output:
        logger.warning(
            "[USB/IP] Ubuntu source inventory failed: %s",
            (stderr or "").strip(),
        )
        return []
    devices: list[dict[str, Any]] = []
    for properties in parse_udev_property_blocks(output):
        busid = predict_linux_usbip_busid(
            properties.get("BUSNUM", ""),
            properties.get("DEVNUM", ""),
            properties.get("DEVPATH", ""),
        )
        if not busid:
            continue
        vendor = (properties.get("ID_VENDOR_ID") or "").lower()
        model = (properties.get("ID_MODEL_ID") or "").lower()
        if not vendor or not model:
            continue
        if (vid_pids or markers) and not _device_matches_android(
            properties, vid_pids, markers
        ):
            continue
        vendor_name = (
            properties.get("ID_VENDOR_FROM_DATABASE")
            or properties.get("ID_VENDOR")
            or vendor
        )
        model_name = (
            properties.get("ID_MODEL_FROM_DATABASE")
            or properties.get("ID_MODEL")
            or model
        )
        vid_pid = f"{vendor}:{model}"
        devices.append({
            "busid": busid,
            "serial": (properties.get("ID_SERIAL_SHORT") or "").strip(),
            "vid_pid": vid_pid,
            "label": f"{busid}: {vendor_name} {model_name} ({vid_pid})",
            "location_path": (properties.get("DEVPATH") or "").strip(),
        })
    return devices


def resolve_linux_usbipd_bin(ssh_manager, ssh) -> tuple[str, str]:
    """Locate the newest usable usbipd on the Ubuntu source; return (binary, version).

    候选按 PATH、``/usr/local/bin`` 与用户目录 ``~/.local/bin`` 枚举，取
    版本最高者——无 sudo 主机上部署到用户目录的新版本可覆盖旧的系统安装。
    版本不可读的候选视为不可用，交由调用方触发自动部署。
    """
    command = (
        'for b in "$(command -v usbipd)" /usr/local/bin/usbipd '
        '"$HOME/.local/bin/usbipd"; do '
        '[ -n "$b" ] && [ -x "$b" ] || continue; echo "$b"; done'
    )
    stdout, _stderr, _code = ssh_manager.execute_command(ssh, command, timeout=10)
    best_binary, best_version = "", ""
    best_tuple: tuple[int, ...] = ()
    seen: set[str] = set()
    for line in (stdout or "").splitlines():
        binary = line.strip()
        if not binary or binary in seen:
            continue
        seen.add(binary)
        version_out, _version_err, version_code = ssh_manager.execute_command(
            ssh, f"{binary} --version", timeout=10,
        )
        if version_code != 0:
            continue
        version = (version_out or "").strip()
        parsed = usbipd_version_tuple(version)
        if not parsed:
            continue
        if not best_tuple or parsed > best_tuple:
            best_binary, best_version, best_tuple = binary, version, parsed
    return best_binary, best_version


def usbipd_version_tuple(version: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", str(version or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def stop_ubuntu_usbip_server(ssh_manager, ssh) -> dict[str, Any]:
    """Stop the userspace usbipd server on the Ubuntu source."""
    running = query_ubuntu_running_usbipd(ssh_manager, ssh)
    if not running["running"]:
        return {"success": True, "stopped": False, "detail": "usbipd未运行"}
    _stop_linux_usbipd(ssh_manager, ssh)
    for _ in range(5):
        time.sleep(0.5)
        if not query_ubuntu_running_usbipd(ssh_manager, ssh)["running"]:
            return {"success": True, "stopped": True}
    _stop_linux_usbipd(ssh_manager, ssh, signal_flag="-9")
    time.sleep(1)
    still_running = query_ubuntu_running_usbipd(ssh_manager, ssh)["running"]
    return {
        "success": not still_running,
        "stopped": not still_running,
        "detail": "usbipd进程无法停止" if still_running else "已强制停止",
    }


def _read_log_tail(ssh_manager, ssh, lines: int = 20) -> str:
    stdout, _stderr, _code = ssh_manager.execute_command(
        ssh, f"tail -n {lines} {shlex.quote(LINUX_USBIPD_LOG)} 2>/dev/null", timeout=10,
    )
    return (stdout or "").strip()


def _usbipd_supports_listen(ssh_manager, ssh, binary: str) -> bool:
    """Probe whether the deployed usbipd accepts a --listen option."""
    stdout, _stderr, _code = ssh_manager.execute_command(
        ssh, f"{shlex.quote(binary)} --version", timeout=5,
    )
    # 0.9.3+ 内置 --listen；更低版本无该选项（usage/版本输出无从判断时
    # 保守返回 False，保持旧版 0.0.0.0 行为，由网络层防火墙兜底）。
    parsed = usbipd_version_tuple(stdout or "")
    return parsed >= (0, 9, 3)


def _resolve_listen_address(ssh_manager, ssh) -> str:
    """Return ``<ip>:3240`` where <ip> is the SSH-reachable source address.

    使用 SSH 连接本侧看到的对端 IP（SSH_CONNECTION 的第三列），即 Controller/
    Worker 实际访问来源主机所走的地址；只绑这个地址可避免 0.0.0.0 把
    内网/管理接口一并暴露。
    """
    stdout, _stderr, _code = ssh_manager.execute_command(
        ssh, "echo $SSH_CONNECTION", timeout=5,
    )
    fields = (stdout or "").split()
    if len(fields) >= 3:
        return f"{fields[2]}:3240"
    return "0.0.0.0:3240"


def ensure_ubuntu_usbip_server(
    ssh_manager,
    ssh,
    serials: list[str] | tuple[str, ...] = (),
    vids: list[str] | tuple[str, ...] = (),
    stop_adb: bool = True,
) -> dict[str, Any]:
    """Start (or reuse) the usbipd server exporting the requested devices.

    已有实例按串号覆盖请求时直接复用；串号不足时合并新旧过滤器重启，
    避免杀掉其他设备正在使用的导出进程。串号为空时退化为按 VID 导出。

    来源缺少可用 usbipd（未安装/版本过低）时，先自动部署随平台分发的
    ``tools/usbipd``（见 :func:`install_ubuntu_usbipd`），部署成功后继续。
    """
    binary, version = resolve_linux_usbipd_bin(ssh_manager, ssh)
    parsed = usbipd_version_tuple(version)
    deploy_error = ""
    if not binary or (parsed and parsed < LINUX_USBIPD_MIN_VERSION):
        deployed = install_ubuntu_usbipd(ssh_manager, ssh)
        if deployed.get("success"):
            logger.info(
                "[USB/IP] Auto-deployed bundled usbipd on Ubuntu source: %s",
                deployed.get("version") or "unknown version",
            )
            binary, version = resolve_linux_usbipd_bin(ssh_manager, ssh)
            parsed = usbipd_version_tuple(version)
        else:
            deploy_error = str(deployed.get("error") or "").strip()
    if not binary:
        result = linux_usbipd_not_installed_error()
        if deploy_error:
            result["error"] = f"usbipd未安装，自动部署失败：{deploy_error}"
        return result
    if parsed and parsed < LINUX_USBIPD_MIN_VERSION:
        error = (
            "Ubuntu来源usbipd版本过低（需支持可重复--serial，"
            f"当前 {version}），请重新部署"
        )
        if deploy_error:
            error += f"；自动部署失败：{deploy_error}"
        return {
            "success": False,
            "error": error,
            "install_guide": LINUX_USBIPD_INSTALL_GUIDE,
        }

    needed_serials = sorted({
        str(item or "").strip() for item in serials or () if str(item or "").strip()
    })
    needed_vids = sorted({
        str(item or "").lower().strip() for item in vids or () if str(item or "").strip()
    })

    running = query_ubuntu_running_usbipd(ssh_manager, ssh)
    if running["running"]:
        running_serials = [str(item) for item in running["serials"]]
        running_vids = [str(item) for item in running["vids"]]
        # 统一 coverage 判断：serial 与 vid 两组过滤器都必须被旧实例覆盖
        # 才能复用。只比 serial 会让 "--vid 2207 → 追加 --vid 18d1" 这类
        # 请求被误判为已覆盖，对应设备永远不会出现在 usbip list。
        no_filter_instance = not running_serials and not running_vids
        if no_filter_instance:
            # 无过滤器的实例导出全部设备，覆盖请求的串号/VID。
            return {"success": True, "reused": True, "started": False,
                    "serials": [], "version": version}
        serial_covered = (
            not needed_serials or set(needed_serials) <= set(running_serials)
        )
        # VID 覆盖仅当旧实例带 VID 过滤时才可信：serial-only 实例只导出
        # 串号命中的设备，不能覆盖额外的 VID 请求。
        vid_covered = (
            not needed_vids
            or (bool(running_vids) and set(needed_vids) <= set(running_vids))
        )
        if serial_covered and vid_covered and (
            needed_serials or not running_serials
        ):
            return {"success": True, "reused": True, "started": False,
                    "serials": running_serials, "version": version}
        # 覆盖不足：合并既有过滤器后重启。
        merged_serials = sorted(set(running_serials) | set(needed_serials))
        merged_vids = sorted(set(running_vids) | set(needed_vids))
        stop_result = stop_ubuntu_usbip_server(ssh_manager, ssh)
        if not stop_result.get("success"):
            return {
                "success": False,
                "error": f"无法停止旧usbipd进程: {stop_result.get('detail')}",
            }
        needed_serials = merged_serials
        needed_vids = merged_vids

    if not needed_serials and not needed_vids:
        return {
            "success": False,
            "error": "缺少USB/IP导出过滤器（serial或vid）",
        }
    # usbipd 0.9.2+ 默认只监听 127.0.0.1（安全默认）。Worker 从外部
    # attach，必须显式绑定来源主机的对外地址（SSH 目标地址即来源可达 IP）。
    # 旧版本 usbipd 没有 --listen 选项时保持旧行为（0.0.0.0）。
    listen_addr = ""
    supports_listen = parsed >= (0, 9, 3) or _usbipd_supports_listen(
        ssh_manager, ssh, binary,
    )
    if supports_listen:
        listen_addr = _resolve_listen_address(ssh_manager, ssh)

    # 全部参数走 argv 形式并 shlex.quote：USB serial 是外部设备输入，
    # 二进制路径可能包含用户目录，禁止直接拼进 shell 字符串。
    argv = [binary, "bind"]
    if stop_adb:
        argv.append("--stop-adb")
    if listen_addr:
        argv += ["--listen", listen_addr]
    for serial in needed_serials:
        argv += ["--serial", serial]
    for vid in needed_vids:
        argv += ["--vid", vid]
    command = (
        shlex.join(argv)
        + f" >{shlex.quote(LINUX_USBIPD_LOG)} 2>&1 </dev/null & echo $! > "
        # PID 文件路径含 $HOME，必须保持未引用以允许变量展开。
        + LINUX_USBIPD_PID_FILE
        + "; true"
    )
    # 先确保 state 目录存在再写 PID 文件。
    ssh_manager.execute_command(
        ssh, 'mkdir -p "$HOME/.local/state" 2>/dev/null; true', timeout=5,
    )
    ssh_manager.execute_command(ssh, command, timeout=10)
    for _ in range(10):
        time.sleep(0.5)
        if query_ubuntu_running_usbipd(ssh_manager, ssh)["running"]:
            logger.info(
                "[USB/IP] Ubuntu usbipd started: serials=%s vids=%s",
                needed_serials, needed_vids,
            )
            return {
                "success": True,
                "reused": False,
                "started": True,
                "serials": needed_serials,
                "vids": needed_vids,
                "version": version,
            }
    return {
        "success": False,
        "error": "Ubuntu来源usbipd启动失败",
        "detail": _read_log_tail(ssh_manager, ssh),
    }


def install_ubuntu_usbipd(
    ssh_manager,
    ssh,
    local_binary: str | None = None,
) -> dict[str, Any]:
    """Upload tools/usbipd to the Ubuntu source and install it.

    优先 ``sudo -n install`` 到 ``/usr/local/bin``；主机无免密 sudo 时回退
    安装到用户目录 ``~/.local/bin``（解析时取候选中版本最高者，二者等价）。
    """
    binary = local_binary or str(
        Path(__file__).resolve().parents[2] / "tools" / "usbipd"
    )
    if not Path(binary).is_file():
        return {
            "success": False,
            "error": f"本地usbipd二进制不存在: {binary}",
            "install_guide": LINUX_USBIPD_INSTALL_GUIDE,
        }
    try:
        sftp = ssh.open_sftp()
        try:
            sftp.put(binary, LINUX_USBIPD_UPLOAD)
        finally:
            sftp.close()
    except Exception as exc:
        return {
            "success": False,
            "error": f"上传usbipd失败: {exc}",
            "install_guide": LINUX_USBIPD_INSTALL_GUIDE,
        }

    def _verified(message: str, **extra) -> dict[str, Any]:
        installed_binary, version = resolve_linux_usbipd_bin(ssh_manager, ssh)
        if not installed_binary:
            return {
                "success": False,
                "error": "usbipd安装后仍不可用",
                "install_guide": LINUX_USBIPD_INSTALL_GUIDE,
            }
        return {
            "success": True,
            "message": message.format(version=version or "未知"),
            "version": version,
            **extra,
        }

    _out, err, code = ssh_manager.execute_command(
        ssh,
        f"sudo -n install -m 0755 {LINUX_USBIPD_UPLOAD} {LINUX_USBIPD_BIN}",
        timeout=30,
    )
    if code == 0:
        result = _verified("usbipd 安装成功！版本: {version}")
        if result.get("success"):
            return result
    user_out, user_err, user_code = ssh_manager.execute_command(
        ssh,
        f'mkdir -p "$HOME/.local/bin" '
        f'&& install -m 0755 {LINUX_USBIPD_UPLOAD} "$HOME/.local/bin/usbipd"',
        timeout=15,
    )
    if user_code == 0:
        result = _verified(
            "usbipd 已安装到用户目录 ~/.local/bin（系统目录安装需要sudo）"
            "！版本: {version}",
            user_local=True,
        )
        if result.get("success"):
            return result
    detail = (user_err or err or user_out or _out or "").strip()
    return {
        "success": False,
        "error": (
            "安装usbipd需要sudo权限，且用户目录安装失败: "
            + (detail or "unknown")
        ),
        "install_guide": LINUX_USBIPD_INSTALL_GUIDE,
    }
