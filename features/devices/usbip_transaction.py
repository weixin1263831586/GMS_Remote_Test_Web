"""USB/IP 结构化解析与事务回滚（自 usbip.py 拆出）。

- parse_usbip_port_entries: 把 ``usbip port`` 输出解析成结构化条目，
  detach 按 host/busid 全等匹配，杜绝 substring 误 detach。
- rollback_windows_binds: attach 失败时回滚本次事务在 Windows 源端
  新 bind 的设备，避免"幽灵 bind"残留。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any


logger = logging.getLogger(__name__)


def _resolve_usbip_command() -> str:
    """定位目标侧 usbip 客户端二进制。

    install.sh 的 sudoers 同时放行 ``/usr/sbin/usbip *`` 与
    ``/usr/bin/usbip *``；Ubuntu linux-tools 各版本安装位置不同，
    逐个探测后再回退 PATH（sudo secure_path 含 /usr/sbin）。
    """
    override = os.environ.get("USBIP_BIN", "").strip()
    if override:
        return override
    for candidate in ("/usr/bin/usbip", "/usr/sbin/usbip"):
        if os.path.exists(candidate):
            return candidate
    return "usbip"


USBIP_PORT_COMMAND = f"sudo -n {_resolve_usbip_command()} port"

# usbip port 输出的设备行，例如：
#     1-2 | 05ac:12a8 | Remix Mini | Remote USB/IP host 10.0.0.5
# BUSID 组件：usbipd-win 为 hub 路径（1-2 / 2-1.4），用户态 usbipd
# 服务端为 busnum-address-port（1-17-13），统一接受多段数字。
_USBIP_PORT_DEVICE_RE = re.compile(
    r'^\s*(?P<busid>\d+(?:[-.]\d+)+)\s*\|[^|]*\|[^|]*\|\s*Remote USB/IP host (?P<host>[^\s]+)\s*$'
)
_USBIP_PORT_URL_RE = re.compile(
    r"\b(?P<local_busid>\d+(?:[-.]\d+)+)\s*->\s*"
    r"usbip://(?P<host>\[[0-9A-Fa-f:]+\]|[^/:\s]+)(?::\d+)?/"
    r"(?P<busid>\d+(?:[-.]\d+)+)\b",
    re.IGNORECASE,
)


def parse_usbip_port_entries(output: str) -> list[dict[str, str]]:
    """Parse ``usbip port`` output into structured (port, busid, host) entries.

    detach 匹配必须用结构化字段做全等比较，不能对整段文本做 substring：
    ``10.10.10.1`` 会误匹配 ``10.10.10.10``，busid ``1-2`` 会误匹配 ``1-21``。
    无法解析出行级 host 的条目（格式变化）保持 host 为空字符串，调用方
    按"host 未匹配"处理，宁可漏 detach 也不误 detach 其他主机的端口。
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in (output or '').splitlines():
        port_match = re.match(r'\s*Port\s+(\d+):', line)
        if port_match:
            current = {
                'port': port_match.group(1),
                'busid': '',
                'host': '',
            }
            entries.append(current)
            continue
        if current is None:
            continue
        device_match = _USBIP_PORT_DEVICE_RE.match(line)
        if device_match:
            current.update({
                'busid': device_match.group('busid'),
                'host': device_match.group('host').strip('[]'),
            })
            continue
        url_match = _USBIP_PORT_URL_RE.search(line)
        if url_match:
            current.update({
                'busid': url_match.group('busid'),
                'host': url_match.group('host').strip('[]'),
            })
    return entries


def usbip_attached_ports(ssh_manager, ssh) -> set[str]:
    """Return the set of currently attached usbip port numbers (as strings)."""
    result = ssh_manager.execute_command(
        ssh, USBIP_PORT_COMMAND, timeout=10
    )
    if not result.ok:
        return set()
    # parse_usbip_port_entries keeps every Port header even when a future
    # usbip version changes the detail-line format.  This makes the
    # post-detach confirmation fail closed: an unparsed-but-present port is
    # never mistaken for a successfully removed port.
    return {entry['port'] for entry in parse_usbip_port_entries(result.stdout or '')}


def detach_ubuntu_usbip_ports(
    ssh_manager,
    ssh,
    remote_host: str | None = '127.0.0.1',
    detach_all: bool = False,
    busids: list[str] | None = None,
) -> list[str]:
    """Detach Ubuntu usbip ports that point to a remote USB/IP host.

    匹配规则：host 与 busid 均为结构化字段全等比较（见
    parse_usbip_port_entries），避免 substring 误 detach 其他主机或
    其他设备的端口。
    """
    detached: list[str] = []
    port_result = ssh_manager.execute_command(
        ssh, USBIP_PORT_COMMAND, timeout=10
    )
    if not port_result.ok:
        logger.info(
            f"[USB/IP] usbip port returned {port_result.code}: "
            f"{port_result.stderr or port_result.stdout}"
        )
        return detached

    target_busids = {str(item) for item in (busids or [])}
    for entry in parse_usbip_port_entries(port_result.stdout or ''):
        current_port = entry['port']
        if not detach_all:
            host_matches = bool(remote_host) and entry['host'] == str(remote_host)
            busid_matches = not target_busids or entry['busid'] in target_busids
            if not (host_matches and busid_matches):
                continue
        detach_result = ssh_manager.execute_command(
            ssh, f'sudo usbip detach -p {current_port}', timeout=15
        )
        # 仅当 detach 命令成功或端口确实已消失时才计入 detached，
        # 否则调用方会误以为端口已释放并继续 attach。
        port_gone = current_port not in usbip_attached_ports(ssh_manager, ssh)
        if detach_result.ok or port_gone:
            detached.append(current_port)
        else:
            logger.warning(
                f"[USB/IP] Failed to detach Ubuntu usbip port {current_port}: "
                f"code={detach_result.code} out={detach_result.stdout} "
                f"err={detach_result.stderr}"
            )

    if detached:
        time.sleep(2)
    return detached


def rollback_ubuntu_attachments(
    ssh_manager,
    ssh,
    remote_host: str,
    busids: list[str],
) -> bool:
    """Detach target-side ports for this transaction and verify they are gone."""
    detach_ubuntu_usbip_ports(
        ssh_manager,
        ssh,
        remote_host=remote_host,
        busids=busids,
    )
    verify_result = ssh_manager.execute_command(
        ssh, USBIP_PORT_COMMAND, timeout=10
    )
    if not verify_result.ok:
        logger.warning(
            "[USB/IP] Cannot verify target rollback: %s",
            (verify_result.stderr or verify_result.stdout or "").strip(),
        )
        return False
    entries = parse_usbip_port_entries(verify_result.stdout or '')
    if any(not entry['host'] or not entry['busid'] for entry in entries):
        logger.warning(
            "[USB/IP] Cannot verify target rollback: unparsed attached port remains"
        )
        return False
    selected = {str(item) for item in busids}
    remaining = [
        entry
        for entry in entries
        if entry['host'] == str(remote_host)
        and entry['busid'] in selected
    ]
    if remaining:
        logger.warning(
            "[USB/IP] Target rollback incomplete; ports remain: %s",
            ", ".join(entry['port'] for entry in remaining),
        )
        return False
    return True


def rollback_windows_binds(
    ssh_manager,
    win_ssh,
    newly_bound: list[str],
) -> bool:
    """Undo Windows-side binds created by the current start_usbip attempt.

    事务回滚的 source 端：attach 在 Ubuntu 侧失败时，Windows 上残留的
    bind 会成为"幽灵 bind"。复用 prepare/bind 阶段仍然存活的 SSH，
    避免失败路径再次建连；只回滚本次新 bind 的 busid。返回回滚是否完整。
    """
    if not newly_bound:
        return True
    rolled_back: list[str] = []
    if not win_ssh:
        return False
    try:
        for busid in newly_bound:
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(busid or "")):
                continue
            ssh_manager.execute_command(
                win_ssh, f"usbipd detach --busid {busid}", timeout=15
            )
            unbind_result = ssh_manager.execute_command(
                win_ssh, f"usbipd unbind --busid {busid}", timeout=15
            )
            if unbind_result.ok:
                rolled_back.append(busid)
            else:
                logger.warning(
                    "[USB/IP] Rollback unbind failed for %s: %s",
                    busid,
                    (unbind_result.stderr or "").strip(),
                )
    except Exception as e:
        logger.warning(f"[USB/IP] Rollback of Windows binds failed: {e}")
    complete = set(rolled_back) == set(newly_bound)
    if not complete:
        logger.warning(
            "[USB/IP] Windows bind rollback incomplete: %s",
            ", ".join(sorted(set(newly_bound) - set(rolled_back))),
        )
    else:
        logger.info("[USB/IP] Rolled back Windows binds: %s", ", ".join(rolled_back))
    return complete


def usbip_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    remediation: str = "",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": code,
        "error": message,
        "retryable": retryable,
        "remediation": remediation,
        **extra,
    }
