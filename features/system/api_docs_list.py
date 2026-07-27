"""
API_DOCS_LIST - API文档列表定义

此文件包含所有API端点的文档信息，用于：
1. 生成 /api/system/docs 接口的响应
2. 提供API帮助文本
3. 技能命令参考

与前端 static/js/api-constants.js 中的 API_DETAILS_MAP 保持对应关系。
"""

# ==================== 常量定义 ====================

SKILL_COMMAND_PREFIX = "gms-rt-"

# ==================== API文档列表 ====================

API_DOCS_LIST = [
    # ==================== 基础接口 ====================
    {
        "method": "GET",
        "path": "/",
        "description": "获取首页（Web界面）",
        "params": [],
        "skill": f"{SKILL_COMMAND_PREFIX}docs"
    },
    {
        "method": "GET",
        "path": "/api/system/health",
        "description": "系统管理",
        "params": [],
        "category": "system",
        "skill": f"{SKILL_COMMAND_PREFIX}system-health"
    },

    # ==================== 配置管理 ====================
    {
        "method": "GET",
        "path": "/api/config/read",
        "description": "获取完整系统配置（包含所有字段和敏感信息）",
        "params": [],
        "category": "config",
        "skill": "gms-rt-config-read"
    },
    {
        "method": "POST",
        "path": "/api/config/update",
        "description": "更新系统配置（仅修改动态配置字段）",
        "params": [
            {"name": "local_server", "type": "string", "required": False, "desc": "本地服务器地址"},
            {"name": "client_hosts", "type": "object", "required": False, "desc": "客户端主机映射 {ip: username}"},
            {"name": "client_ssh_credentials", "type": "array", "required": False, "desc": "客户端SSH凭证列表"}
        ],
        "category": "config",
        "skill": "gms-rt-config-update"
    },

    # ==================== 用户管理 ====================
    {
        "method": "GET",
        "path": "/api/users/current",
        "description": "获取当前用户信息（返回client_id用于WebSocket连接）",
        "params": [],
        "category": "users",
        "skill": "gms-rt-users-current"
    },
    {
        "method": "POST",
        "path": "/api/users/detect",
        "description": "检测客户端用户信息（通过SSH自动识别）",
        "params": [
            {"name": "ip", "type": "string", "required": False, "desc": "客户端IP地址（可选）"},
            {"name": "username", "type": "string", "required": False, "desc": "用户名（可选）"},
            {"name": "password", "type": "string", "required": False, "desc": "密码（可选）"}
        ],
        "category": "users",
        "skill": "gms-rt-users-detect"
    },
    {
        "method": "POST",
        "path": "/api/users/set-username",
        "description": "手动设置客户端用户名（无需SSH密码）",
        "params": [
            {"name": "username", "type": "string", "required": True, "desc": "用户名（不能为unknown）"},
            {"name": "ip", "type": "string", "required": False, "desc": "客户端IP地址（可选，默认自动获取）"}
        ],
        "category": "users",
        "skill": "gms-rt-users-set-username"
    },
    {
        "method": "GET",
        "path": "/api/users/list",
        "description": "获取所有在线用户列表",
        "params": [],
        "category": "users",
        "skill": "gms-rt-users-list"
    },

    # ==================== 设备管理 ====================
    {
        "method": "GET",
        "path": "/api/devices/list",
        "description": "获取Android设备列表",
        "params": [
            {"name": "force_refresh", "type": "number", "required": False, "desc": "是否强制刷新，默认0"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-list"
    },
    {
        "method": "POST",
        "path": "/api/devices/bootloader-lock",
        "description": "锁定设备Bootloader",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-bootloader-lock"
    },
    {
        "method": "POST",
        "path": "/api/devices/bootloader-unlock",
        "description": "解锁设备Bootloader",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-bootloader-unlock"
    },
    {
        "method": "POST",
        "path": "/api/devices/bootloader-status",
        "description": "检查设备的Verified Boot锁定状态(GREEN=锁定, ORANGE=未锁定)",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-bootloader-status"
    },
    {
        "method": "POST",
        "path": "/api/devices/info",
        "description": "获取设备的详细硬件和软件信息",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-info"
    },
    {
        "method": "GET",
        "path": "/api/devices/management",
        "description": "获取所有设备的详细管理信息(设备列表、电池、来源等)",
        "params": [],
        "category": "device"
    },
    {
        "method": "GET",
        "path": "/api/devices/user-locked",
        "description": "列出用户锁定设备",
        "params": [],
        "category": "device",
        "skill": "gms-rt-devices-user-locked"
    },
    {
        "method": "POST",
        "path": "/api/devices/reboot",
        "description": "重启设备",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-reboot"
    },
    {
        "method": "POST",
        "path": "/api/devices/remount",
        "description": "将设备重新挂载为读写模式",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-remount"
    },
    {
        "method": "POST",
        "path": "/api/devices/wifi",
        "description": "让设备连接到指定的WiFi网络",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"},
            {"name": "ssid", "type": "string", "required": False, "desc": "WiFi名称，留空读取 config.wifi.ssid"},
            {"name": "password", "type": "string", "required": False, "desc": "WiFi密码，留空读取 config.wifi.password"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-wifi"
    },
    {
        "method": "POST",
        "path": "/api/devices/shell",
        "description": "在设备上执行ADB Shell命令",
        "params": [
            {"name": "serial_no", "type": "string", "required": True, "desc": "设备序列号"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-shell"
    },
    {
        "method": "POST",
        "path": "/api/devices/scrcpy",
        "description": "启动设备屏幕显示",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"}
        ],
        "category": "device",
        "skill": "gms-rt-devices-scrcpy"
    },
    # ==================== 测试管理 ====================
    {
        "method": "POST",
        "path": "/api/test/start",
        "description": "启动GMS测试(CTS/VTS/GTS等)",
        "params": [
            {"name": "devices", "type": "array", "required": True, "desc": "设备序列号数组"},
            {"name": "test_type", "type": "string", "required": True, "desc": "测试类型: CTS|VTS|STS|GTS|CTS_VERIFIER"},
            {"name": "test_module", "type": "string", "required": True, "desc": "测试模块名称"},
            {"name": "test_case", "type": "string", "required": False, "desc": "具体测试用例(可选)"},
            {"name": "retry_dir", "type": "string", "required": False, "desc": "重试目录(可选)"},
            {"name": "test_suite", "type": "string", "required": False, "desc": "测试套件路径(可选)"}
        ],
        "category": "test",
        "skill": "gms-rt-test-start"
    },
    {
        "method": "POST",
        "path": "/api/test/stop",
        "description": "停止测试",
        "params": [],
        "category": "test",
        "skill": "gms-rt-test-stop"
    },
    {
        "method": "GET",
        "path": "/api/test/suites",
        "description": "列出可用的测试套件",
        "params": [],
        "category": "test",
        "skill": "gms-rt-test-suites"
    },
    {
        "method": "POST",
        "path": "/api/test/clean",
        "description": "清理测试环境",
        "params": [],
        "category": "test",
        "skill": "gms-rt-test-clean"
    },
    {
        "method": "POST",
        "path": "/api/test/suites/result",
        "description": "列出测试套件结果（tradefed list results）- 使用原生输出格式",
        "params": ["suite_path", "tradefed_bin"],
        "category": "test",
        "skill": "gms-rt-test-suites-result"
    },
    {
        "method": "GET",
        "path": "/api/test/status",
        "description": "获取当前测试运行状态",
        "params": [],
        "category": "test",
        "skill": "gms-rt-test-status"
    },
    {
        "method": "POST",
        "path": "/api/test/logs/save",
        "description": "保存当前正在运行的日志",
        "params": [],
        "category": "test"
    },
    {
        "method": "GET",
        "path": "/api/test/logs/stream",
        "description": "实时流式输出测试日志",
        "params": [],
        "category": "test",
        "skill": "gms-rt-test-logs-stream"
    },

    # ==================== 报告管理 ====================
    {
        "method": "GET",
        "path": "/api/reports/list",
        "description": "获取所有历史测试报告",
        "params": [],
        "category": "report",
        "skill": "gms-rt-reports-list"
    },
    {
        "method": "POST",
        "path": "/api/reports/analyze",
        "description": "统一的报告分析 API（支持上传、已保存报告、AI 分析）",
        "params": [
            {"name": "mode", "type": "string", "required": True, "desc": "分析模式：upload/saved/ai"},
            {"name": "file", "type": "file", "required": False, "desc": "上传的文件（mode=upload 时）"},
            {"name": "report_timestamp", "type": "string", "required": False, "desc": "报告时间戳（mode=saved 时）"},
            {"name": "test_name", "type": "string", "required": False, "desc": "测试用例名（mode=ai 时）"},
            {"name": "error_message", "type": "string", "required": False, "desc": "错误消息（mode=ai 时）"}
        ],
        "category": "report",
        "skill": "gms-rt-reports-analyze"
    },
    {
        "method": "GET",
        "path": "/api/reports/download",
        "description": "获取报告文件列表、下载ZIP或查看文件内容（统一接口）",
        "params": [
            {"name": "report_timestamp", "type": "string", "required": False, "desc": "报告时间戳（获取文件列表）"},
            {"name": "download", "type": "boolean", "required": False, "desc": "设为true时下载ZIP文件"},
            {"name": "path", "type": "string", "required": False, "desc": "文件路径（查看单个文件内容）"}
        ],
        "category": "report",
        "skill": "gms-rt-reports-download"
    },
    {
        "method": "DELETE",
        "path": "/api/reports/delete",
        "description": "删除指定的测试报告",
        "params": [
            {"name": "report_timestamp", "type": "string", "required": True, "desc": "报告时间戳"}
        ],
        "category": "report",
        "skill": "gms-rt-reports-delete"
    },

    # ==================== 桌面管理 ====================
    {
        "method": "GET",
        "path": "/api/desktop/vnc/status",
        "description": "查询Ubuntu主机桌面VNC状态",
        "params": [],
        "category": "desktop",
        "skill": "gms-rt-desktop-vnc-status"
    },
    {
        "method": "POST",
        "path": "/api/desktop/vnc/start",
        "description": "启动Ubuntu主机桌面VNC服务",
        "params": [
            {"name": "host", "type": "string", "required": False, "desc": "Ubuntu主机桌面地址，格式：user@ip"},
            {"name": "password", "type": "string", "required": False, "desc": "SSH登录密码"},
            {"name": "vnc_password", "type": "string", "required": False, "desc": "VNC访问密码（可选）"}
        ],
        "category": "desktop",
        "skill": "gms-rt-desktop-vnc-start"
    },
    {
        "method": "POST",
        "path": "/api/desktop/vnc/stop",
        "description": "停止Ubuntu主机桌面VNC服务",
        "params": [],
        "category": "desktop",
        "skill": "gms-rt-desktop-vnc-stop"
    },
    {
        "method": "POST",
        "path": "/api/desktop/validate",
        "description": "验证Ubuntu主机SSH连接并检查VNC服务可用性",
        "params": [
            {"name": "host", "type": "string", "required": True, "desc": "主机地址（格式：user@ip，如gms@192.168.1.10）"},
            {"name": "password", "type": "string", "required": False, "desc": "SSH登录密码（可选）"}
        ],
        "category": "desktop",
        "skill": "gms-rt-desktop-validate"
    },

    # ==================== SSH管理 ====================
    {
        "method": "GET",
        "path": "/api/ssh/sshd",
        "description": "检查SSH服务状态（如未安装则返回安装指南）",
        "params": [
            {"name": "device_host", "type": "string", "required": False, "desc": "目标主机 (user@ip 或 ip)，不传则使用当前客户端"}
        ],
        "category": "ssh",
        "skill": "gms-rt-ssh-sshd"
    },
    {
        "method": "POST",
        "path": "/api/ssh/ping",
        "description": "测试测试主机和客户端之间的网络连通性（ping 测试）",
        "params": [
            {"name": "test_host_ip", "type": "string", "required": True, "desc": "测试主机 IP 地址"},
            {"name": "client_ip", "type": "string", "required": True, "desc": "客户端 IP 地址"}
        ],
        "category": "ssh",
        "skill": "gms-rt-ssh-ping"
    },
    {
        "method": "GET",
        "path": "/api/ssh/route",
        "description": "检查系统路由表",
        "params": [],
        "category": "ssh",
        "skill": "gms-rt-ssh-route"
    },

    # ==================== VPN管理 ====================
    {
        "method": "GET",
        "path": "/api/vpn/status",
        "description": "检查VPN连接状态",
        "params": [],
        "category": "vpn",
        "skill": "gms-rt-vpn-status"
    },
    {
        "method": "POST",
        "path": "/api/vpn/connect",
        "description": "连接到默认VPN服务器（无需参数）",
        "params": [],
        "category": "vpn",
        "skill": "gms-rt-vpn-connect"
    },
    {
        "method": "POST",
        "path": "/api/vpn/disconnect",
        "description": "断开VPN连接",
        "params": [],
        "category": "vpn",
        "skill": "gms-rt-vpn-disconnect"
    },

    # ==================== USB/IP管理 ====================
    {
        "method": "POST",
        "path": "/api/adb-forward/start",
        "description": "启动ADB端口转发",
        "params": [
            {"name": "device_host", "type": "string", "required": True, "desc": "设备主机地址"},
            {"name": "device_password", "type": "string", "required": True, "desc": "设备SSH密码"}
        ],
        "category": "usbip",
        "skill": "gms-rt-adb-forward-start"
    },
    {
        "method": "POST",
        "path": "/api/adb-forward/stop",
        "description": "停止ADB端口转发",
        "params": [
            {"name": "device_id", "type": "string", "required": True, "desc": "设备序列号"}
        ],
        "category": "usbip",
        "skill": "gms-rt-adb-forward-stop"
    },
    {
        "method": "GET",
        "path": "/api/usbip/status",
        "description": "检查USB/IP服务状态（支持指定主机）",
        "params": [{"name": "device_host", "type": "string", "required": False, "desc": "目标主机 (user@ip 或 ip)，不传则使用当前客户端"}],
        "category": "usbip",
        "skill": "gms-rt-usbip-status"
    },
    {
        "method": "POST",
        "path": "/api/usbip/connect",
        "description": "启动USB/IP设备共享（支持指定主机）",
        "params": [
            {"name": "device_host", "type": "string", "required": True, "desc": "设备主机地址 (user@ip或 ip)"},
            {"name": "device_password", "type": "string", "required": False, "desc": "设备主机SSH密码（可选）"}
        ],
        "category": "usbip",
        "skill": "gms-rt-usbip-connect"
    },
    {
        "method": "POST",
        "path": "/api/usbip/disconnect",
        "description": "停止USB/IP服务（支持指定主机）",
        "params": [{"name": "device_host", "type": "string", "required": False, "desc": "目标主机 (user@ip 或 ip)，不传则使用当前客户端"}],
        "category": "usbip",
        "skill": "gms-rt-usbip-disconnect"
    },
    {
        "method": "POST",
        "path": "/api/usbip/install",
        "description": "安装 USB/IP 服务（支持指定主机）",
        "params": [
            {"name": "device_host", "type": "string", "required": False, "desc": "目标主机 (user@ip 或 ip)，不传则使用当前客户端"}
        ],
        "category": "usbip",
        "skill": "gms-rt-usbip-install"
    },

    # ==================== 文件管理 ====================
    {
        "method": "GET",
        "path": "/api/files/progress",
        "description": "获取当前文件上传进度",
        "params": [
            {"name": "upload_id", "type": "string", "required": False, "desc": "上传任务ID"}
        ],
        "category": "file",
        "skill": "gms-rt-files-progress"
    },
    {
        "method": "POST",
        "path": "/api/files/list",
        "description": "列出设备指定目录的文件",
        "params": [
            {"name": "path", "type": "string", "required": True, "desc": "目录路径，如/sdcard"}
        ],
        "category": "file"
    },

    # ==================== 固件烧写 ====================
    {
        "method": "POST",
        "path": "/api/burn/firmware",
        "description": "烧写固件",
        "params": [
            {"name": "firmware_file", "type": "file", "required": True, "desc": "固件文件（.img格式）"},
            {"name": "devices", "type": "string", "required": True, "desc": "设备序列号（多个用逗号分隔）"},
            {"name": "wipe_data", "type": "boolean", "required": False, "desc": "是否清除数据（默认true）"}
        ],
        "category": "burn",
        "skill": "gms-rt-burn-firmware"
    },
    {
        "method": "POST",
        "path": "/api/burn/gsi",
        "description": "烧写GSI镜像",
        "params": [
            {"name": "gsi_image", "type": "file", "required": True, "desc": "GSI镜像文件（.img格式）"},
            {"name": "devices", "type": "string", "required": True, "desc": "设备序列号（多个用逗号分隔）"},
            {"name": "wipe_data", "type": "boolean", "required": False, "desc": "是否清除数据（默认true）"}
        ],
        "category": "burn",
        "skill": "gms-rt-burn-gsi"
    },
    {
        "method": "POST",
        "path": "/api/burn/serial",
        "description": "烧写设备序列号",
        "params": [
            {"name": "device_id", "type": "string", "required": True, "desc": "当前设备序列号"},
            {"name": "new_serial", "type": "string", "required": True, "desc": "新的序列号"}
        ],
        "category": "burn",
        "skill": "gms-rt-burn-serial"
    },
    {
        "method": "GET",
        "path": "/api/burn/upload-progress",
        "description": "查询固件上传进度",
        "params": [],
        "category": "burn"
    },

    # ==================== 源码搜索 ====================
    {
        "method": "POST",
        "path": "/api/opengrok/search",
        "description": "在源码中搜索代码",
        "params": [
            {"name": "query", "type": "string", "required": True, "desc": "搜索关键词"},
            {"name": "full", "type": "boolean", "required": False, "desc": "是否全文搜索"},
            {"name": "limit", "type": "integer", "required": False, "desc": "最大结果数，范围 1-100，默认 30"}
        ],
        "category": "file",
        "skill": "gms-rt-opengrok-search"
    },

    # ==================== 主机终端 ====================
    {
        "method": "GET",
        "path": "/api/terminal/open",
        "description": "获取SSH终端连接信息，用于建立SSH连接到测试主机",
        "params": [],
        "category": "terminal",
        "skill": "gms-rt-terminal-open",
        "response_example": {
            "success": True,
            "host": "192.168.1.10",
            "user": "gms",
            "port": 22,
            "connection_command": "ssh gms@192.168.1.10",
            "instructions": [
                "1. 复制连接命令: ssh gms@192.168.1.10",
                "2. 在终端中粘贴并执行连接命令",
                "3. 输入密码或使用SSH密钥认证",
                "4. 连接成功后，您将获得测试主机的终端访问权限"
            ]
        }
    },
    {
        "method": "POST",
        "path": "/api/terminal/push",
        "description": "上传文件到测试主机的指定目录（默认 ~/GMS-Suite/tmp，支持分块上传和断点续传）",
        "params": [
            {"name": "file", "type": "file", "required": True, "desc": "要上传的文件"},
            {"name": "path", "type": "string", "required": False, "desc": "目标路径，默认 ~/GMS-Suite/tmp"},
            {"name": "chunk_index", "type": "int", "required": False, "desc": "分块索引（分块上传时使用）"},
            {"name": "total_chunks", "type": "int", "required": False, "desc": "总分块数（分块上传时使用）"},
            {"name": "upload_id", "type": "string", "required": False, "desc": "上传任务ID（分块上传时使用）"}
        ],
        "category": "terminal",
        "skill": "gms-rt-terminal-push",
        "response_example": {
            "success": True,
            "remote_path": "~/GMS-Suite/tmp/filename.ext",
            "message": "文件已上传到 ~/GMS-Suite/tmp/filename.ext"
        }
    },

    # ==================== WebSocket ====================
    {
        "method": "WebSocket",
        "path": "/api/system/websocket/{client_id}",
        "description": "建立WebSocket连接用于实时通信",
        "params": [{"name": "client_id", "type": "string", "required": True}],
        "category": "system",
    },

    # ==================== 技能管理 ====================
    {
        "method": "GET",
        "path": "/api/system/skills",
        "description": "下载Skill ZIP离线包（不安装CLI命令）",
        "params": [],
        "category": "system",
        "skill": "gms-rt-system-skills"
    },
    dict(method="GET", path="/api/system/skills/install.sh", description="获取Skill和独立gms-rt-*命令一键安装脚本", params=[], category="system"),

    # ==================== API文档 ====================
    {
        "method": "GET",
        "path": "/api/system/docs",
        "description": "获取系统接口文档列表",
        "params": [],
        "category": "system",
        "skill": "gms-rt-system-docs"
    },
    {
        "method": "GET",
        "path": "/api/system/help",
        "description": "获取API帮助信息（统一接口）- 不带参数返回所有API列表，带api_path参数返回单个API详细帮助",
        "params": [
            {
                "name": "api_path",
                "type": "Optional[str]",
                "description": "API路径（如 'api/test/start'），不提供则返回所有API列表",
                "required": False
            }
        ],
        "category": "system",
        "skill": "gms-rt-system-help"
    },

    # ==================== APK/源码分析（补充） ====================
    {
        "method": "GET",
        "path": "/api/apk/status/{task_id}",
        "description": "查询APK反编译任务状态（任务ID）",
        "params": [{"name": "task_id", "type": "string", "required": True, "desc": "APK任务ID"}],
        "category": "apk",
        "skill": "gms-rt-apk-status"
    },
    {
        "method": "GET",
        "path": "/api/apk/tasks",
        "description": "列出所有APK反编译任务",
        "params": [],
        "category": "apk",
        "skill": "gms-rt-apk-tasks"
    },
    {
        "method": "GET",
        "path": "/api/apk/manifest/{task_id}",
        "description": "查看APK的AndroidManifest.xml（任务ID）",
        "params": [{"name": "task_id", "type": "string", "required": True, "desc": "APK任务ID"}],
        "category": "apk",
        "skill": "gms-rt-apk-manifest"
    },
    {
        "method": "GET",
        "path": "/api/apk/permissions/{task_id}",
        "description": "查看APK声明的权限列表（任务ID）",
        "params": [{"name": "task_id", "type": "string", "required": True, "desc": "APK任务ID"}],
        "category": "apk",
        "skill": "gms-rt-apk-permissions"
    },
    {
        "method": "GET",
        "path": "/api/apk/source/{task_id}",
        "description": "查看反编译后的源码文件（任务ID，需view/path参数）",
        "params": [
            {"name": "task_id", "type": "string", "required": True, "desc": "APK任务ID"},
            {"name": "path", "type": "string", "required": False, "desc": "源码相对路径"},
            {"name": "view", "type": "boolean", "required": False, "desc": "是否返回内容"}
        ],
        "category": "apk",
        "skill": "gms-rt-apk-source"
    },
    {
        "method": "GET",
        "path": "/api/apk/search/{task_id}",
        "description": "在反编译源码中搜索符号定义（任务ID，需symbol参数）",
        "params": [
            {"name": "task_id", "type": "string", "required": True, "desc": "APK任务ID"},
            {"name": "symbol", "type": "string", "required": True, "desc": "类名/方法名等符号"}
        ],
        "category": "apk",
        "skill": "gms-rt-apk-search"
    },
    {
        "method": "GET",
        "path": "/api/apk/definition/{task_id}",
        "description": "查找符号的定义位置（任务ID，需symbol参数）",
        "params": [
            {"name": "task_id", "type": "string", "required": True, "desc": "APK任务ID"},
            {"name": "symbol", "type": "string", "required": True, "desc": "符号名"}
        ],
        "category": "apk",
        "skill": "gms-rt-apk-definition"
    },
    {
        "method": "POST",
        "path": "/api/apk/analyze/{task_id}",
        "description": "启动APK反编译任务（任务ID）",
        "params": [{"name": "task_id", "type": "string", "required": True, "desc": "APK任务ID"}],
        "category": "apk",
        "skill": "gms-rt-apk-analyze"
    },

    # ==================== 报告（补充） ====================
    {
        "method": "POST",
        "path": "/api/reports/diagnose",
        "description": "诊断报告中的失败用例（根因分析）",
        "params": [
            {"name": "test_name", "type": "string", "required": True, "desc": "测试名"},
            {"name": "error_message", "type": "string", "required": False, "desc": "错误信息"},
            {"name": "stack_trace", "type": "string", "required": False, "desc": "堆栈"},
            {"name": "report_name", "type": "string", "required": False, "desc": "报告时间戳"}
        ],
        "category": "report",
        "skill": "gms-rt-reports-diagnose"
    },
    {
        "method": "POST",
        "path": "/api/reports/analyze-url",
        "description": "从URL分析测试报告",
        "params": [{"name": "url", "type": "string", "required": True, "desc": "报告URL"}],
        "category": "report",
        "skill": "gms-rt-reports-analyze-url"
    },
    {
        "method": "POST",
        "path": "/api/reports/extract-redmine-attachment",
        "description": "从Redmine附件提取报告信息",
        "params": [],
        "category": "report",
        "skill": "gms-rt-reports-extract-redmine"
    },

    # ==================== 知识库 ====================
    {
        "method": "GET",
        "path": "/api/knowledgebase/search",
        "description": "知识库搜索（按关键词检索）",
        "params": [
            {"name": "q", "type": "string", "required": True, "desc": "搜索关键词"},
            {"name": "limit", "type": "integer", "required": False, "desc": "返回条数"}
        ],
        "category": "file",
        "skill": "gms-rt-kb-search"
    },
    {
        "method": "GET",
        "path": "/api/knowledgebase/stats",
        "description": "知识库统计信息",
        "params": [],
        "category": "file",
        "skill": "gms-rt-kb-stats"
    },

    # ==================== 测试日志/套件（补充） ====================
    {
        "method": "GET",
        "path": "/api/test/logs/list",
        "description": "列出测试日志文件",
        "params": [],
        "category": "test",
        "skill": "gms-rt-test-logs-list"
    },
    {
        "method": "GET",
        "path": "/api/test/logs/get",
        "description": "读取指定测试日志内容",
        "params": [{"name": "name", "type": "string", "required": True, "desc": "日志文件名"}],
        "category": "test",
        "skill": "gms-rt-test-logs-get"
    },
    {
        "method": "POST",
        "path": "/api/test/suites/files",
        "description": "列出测试套件目录下的文件",
        "params": [{"name": "path", "type": "string", "required": False, "desc": "套件路径"}],
        "category": "test",
        "skill": "gms-rt-suites-files"
    },
    {
        "method": "GET",
        "path": "/api/test/suites/archives",
        "description": "列出测试套件归档文件",
        "params": [],
        "category": "test",
        "skill": "gms-rt-suites-archives"
    },
    {
        "method": "POST",
        "path": "/api/test/suites/diagnose-target",
        "description": "根据失败信息定位套件中的APK/JAR构件",
        "params": [],
        "category": "test",
        "skill": "gms-rt-suites-diagnose-target"
    },
    {
        "method": "GET",
        "path": "/api/test/suites/download-status/{task_id}",
        "description": "查询套件下载任务状态（任务ID）",
        "params": [{"name": "task_id", "type": "string", "required": True, "desc": "下载任务ID"}],
        "category": "test",
        "skill": "gms-rt-suites-download-status"
    },
    {
        "method": "GET",
        "path": "/api/test/suites/extract-status/{task_id}",
        "description": "查询套件解压任务状态（任务ID）",
        "params": [{"name": "task_id", "type": "string", "required": True, "desc": "解压任务ID"}],
        "category": "test",
        "skill": "gms-rt-suites-extract-status"
    },

    # ==================== 通知 ====================
    {
        "method": "GET",
        "path": "/api/notifications",
        "description": "获取通知列表",
        "params": [],
        "category": "notification",
        "skill": "gms-rt-notifications"
    },
    {
        "method": "POST",
        "path": "/api/notifications/mark-read",
        "description": "标记通知为已读",
        "params": [{"name": "id", "type": "string", "required": False, "desc": "通知ID，不传则全部已读"}],
        "category": "notification",
        "skill": "gms-rt-notifications-mark-read"
    },
    {
        "method": "POST",
        "path": "/api/notifications/clear",
        "description": "清空通知",
        "params": [],
        "category": "notification",
        "skill": "gms-rt-notifications-clear"
    },

    # ==================== 安全审计 ====================
    {
        "method": "GET",
        "path": "/api/security-audit/logs",
        "description": "查询安全审计/访问日志",
        "params": [
            {"name": "limit", "type": "integer", "required": False, "desc": "返回条数"},
            {"name": "action_type", "type": "string", "required": False, "desc": "事件类型过滤（api/page_view/page_visit）"}
        ],
        "category": "audit",
        "skill": "gms-rt-audit-logs"
    },
    {
        "method": "GET",
        "path": "/api/security-audit/detail/{event_id}",
        "description": "查看审计事件详情（事件ID）",
        "params": [{"name": "event_id", "type": "string", "required": True, "desc": "事件ID"}],
        "category": "audit",
        "skill": "gms-rt-audit-detail"
    },
    {
        "method": "GET",
        "path": "/api/security-audit/export",
        "description": "导出安全审计日志",
        "params": [],
        "category": "audit",
        "skill": "gms-rt-audit-export"
    },

    # ==================== 网址/工具/Favicon（assets 补充） ====================
    {
        "method": "GET",
        "path": "/api/websites/load",
        "description": "加载常用网址列表",
        "params": [],
        "category": "assets",
        "skill": "gms-rt-websites-load"
    },
    {
        "method": "GET",
        "path": "/api/tools/list",
        "description": "列出常用工具",
        "params": [],
        "category": "assets",
        "skill": "gms-rt-tools-list"
    },

    # ==================== Tailscale ====================
    {
        "method": "GET",
        "path": "/api/tailscale/status",
        "description": "查询Tailscale VPN状态",
        "params": [],
        "category": "vpn",
        "skill": "gms-rt-tailscale-status"
    },

    # ==================== 配置（补充） ====================
    {
        "method": "GET",
        "path": "/api/config/ai",
        "description": "获取AI模型配置",
        "params": [],
        "category": "config",
        "skill": "gms-rt-config-ai"
    },
    {
        "method": "GET",
        "path": "/api/config/opengrok",
        "description": "获取OpenGrok源码搜索配置",
        "params": [],
        "category": "config",
        "skill": "gms-rt-config-opengrok"
    },
    {
        "method": "GET",
        "path": "/api/config/redmine",
        "description": "获取Redmine看板配置",
        "params": [],
        "category": "config",
        "skill": "gms-rt-config-redmine"
    },

    # ==================== VPN/连接（补充） ====================
    {
        "method": "GET",
        "path": "/api/vpn/connections",
        "description": "获取VPN连接列表",
        "params": [],
        "category": "vpn",
        "skill": "gms-rt-vpn-connections"
    }
]
