from __future__ import annotations


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
    for encoding in ('utf-8', 'gbk', 'latin-1'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')
