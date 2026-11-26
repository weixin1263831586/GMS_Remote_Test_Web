from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


# Windows 客户端登录和系统 SSH 管理都会展示同一份安装指南。将文案放在
# foundation，避免 auth 功能反向依赖 system 功能的内部实现。
SSHD_INSTALL_GUIDE = """以【管理员身份】运行 PowerShell, 按照下面步骤安装:

1️⃣ 安装sshd
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH*'

2️⃣ 启动sshd
Start-Service sshd

3️⃣ 设置sshd开机自启动
Set-Service -Name sshd -StartupType 'Automatic'

⚠️ 若上述步骤安装失败，先以【管理员身份】执行卸载操作，再执行上面的安装步骤

1️⃣ 卸载sshd
Get-Service sshd | Stop-Service -Force
Remove-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

2️⃣ 删除残留文件
Remove-Item -Path "C:\\ProgramData\\ssh" -Recurse -Force -ErrorAction SilentlyContinue

3️⃣ 重启计算机
Restart-Computer
"""


def decode_ssh_output(data: bytes) -> str:
    """Decode UTF-8 or GBK SSH output, with lossless Latin-1 byte fallback."""
    for encoding in ('utf-8', 'gbk'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('latin-1')


def find_device_host_password(
    device_host: str,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Resolve an encrypted credential for the exact user and host."""
    if not config or "@" not in device_host:
        return None
    username, hostname = (
        item.strip() for item in device_host.split("@", 1)
    )
    if not username or not hostname:
        return None
    from foundation.secrets import decrypt_secret

    for credential in config.get("client_ssh_credentials", []):
        if not isinstance(credential, dict):
            continue
        credential_host = str(credential.get("device_host") or "").strip()
        credential_username = str(credential.get("username") or "").strip()
        credential_hostname = str(
            credential.get("host") or credential.get("hostname") or ""
        ).strip()
        if credential_host != device_host and (
            credential_username != username or credential_hostname != hostname
        ):
            continue
        encrypted = str(credential.get("encrypted_password") or "")
        if not encrypted:
            if credential.get("password"):
                logger.warning(
                    "Ignoring plaintext SSH credential for %s; rotate it",
                    device_host,
                )
            return None
        try:
            return decrypt_secret(encrypted)
        except RuntimeError:
            logger.warning(
                "SSH credential for %s cannot be decrypted; rotate it",
                device_host,
            )
            return None
    return None
