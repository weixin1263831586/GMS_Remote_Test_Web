/**
 * API Constants and Configuration
 *
 * This file contains all API-related constants including:
 * - API_CATEGORIES: Maps API endpoints to functional categories
 * - API_DETAILS_MAP: Detailed documentation for each API endpoint
 * - Helper functions for category management
 *
 * Separated from app.js for better maintainability and reusability.
 */

// ==================== API Categories ====================

/**
 * API endpoint to category mapping
 * Used for grouping and filtering endpoints in the API documentation
 */
const API_CATEGORIES = {
    '/api/system/health': 'system',
    '/api/system/help': 'system',
    '/api/system/docs': 'system',
    '/api/config/read': 'config',
    '/api/config/update': 'config',
    '/api/config/values': 'config',
    '/api/users': 'users',
    '/api/devices/list': 'device',
    '/api/devices/bootloader-lock': 'device',
    '/api/devices/bootloader-unlock': 'device',
    '/api/devices/bootloader-status': 'device',
    '/api/devices/info': 'device',
    '/api/devices/management': 'device',
    '/api/devices/user-locked': 'device',
    '/api/devices/reboot': 'device',
    '/api/devices/remount': 'device',
    '/api/devices/wifi-connect': 'device',
    '/api/devices/shell': 'device',
    '/api/devices/screen': 'device',
    '/api/desktop': 'desktop',
    '/api/test': 'test',
    '/api/reports': 'report',
    '/api/vpn': 'vpn',
    '/api/ssh': 'ssh',
    '/api/terminal': 'terminal',
    '/api/adb-forward': 'usbip',
    '/api/usbip': 'usbip',
    '/api/files': 'file',
    '/api/burn': 'burn',
    '/api/system/websocket/': 'system',
    '/api/system/skills': 'system'
};

/**
 * Get API category from path
 * @param {string} path - API endpoint path
 * @returns {string} Category name
 */
function getApiCategory(path) {
    for (const [prefix, category] of Object.entries(API_CATEGORIES)) {
        if (path.startsWith(prefix)) {
            return category;
        }
    }
    return 'other';
}

/**
 * Get display name for category
 * @param {string} category - Category key
 * @returns {string} Display name with emoji
 */
function getCategoryName(category) {
    const names = {
        'test': '🧪 测试管理',
        'config': '⚙️ 配置管理',
        'device': '📱 设备管理',
        'users': '👥 用户管理',
        'client': '👤 用户管理',
        'report': '📊 报告管理',
        'vpn': '🔐 VPN管理',
        'ssh': '🔑 SSH管理',
        'desktop': '🖥️ 主机桌面',
        'terminal': '🐧 主机终端',
        'usbip': '📡 USB/IP',
        'burn': '🔥 固件烧写',
        'file': '📁 文件管理',
        'system': '💚 系统管理',
        'other': '📋 其他'
    };
    return names[category] || '📋 其他';
}

/**
 * Get category sort order
 * @param {string} category - Category key
 * @returns {number} Sort order (lower = first)
 */
function getCategoryOrder(category) {
    const order = {
        'test': 1,
        'config': 2,
        'device': 3,
        'users': 4,
        'client': 4,
        'report': 5,
        'vpn': 6,
        'ssh': 7,
        'desktop': 8,
        'terminal': 9,
        'usbip': 10,
        'burn': 11,
        'file': 12,
        'system': 14,
        'other': 999
    };
    return order[category] || 999;
}

/**
 * Sort APIs by category
 * @param {Array} apis - Array of API objects
 * @returns {Array} Sorted array
 */
function sortApisByCategory(apis) {
    // 全部分类时，直接按路径字母顺序排序
    return apis.sort((a, b) => a.path.localeCompare(b.path));
}

// ==================== API Details Map ====================

/**
 * Detailed information for each API endpoint
 * Used for generating API documentation and help text
 *
 * Each entry contains:
 * - title: Short display name
 * - description: Detailed description
 * - params: Array of parameter objects (optional)
 * - response: Example response structure
 * - usage: Usage example or command
 */
const API_DETAILS_MAP = {
    '/api/test/start': {
        title: '启动测试',
        description: '启动GMS测试(CTS/GSI/GTS/STS/VTS/APTS)',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' },
            { name: 'test_type', type: 'string', required: true, desc: '测试类型: CTS|VTS|STS|GTS|CTS_VERIFIER' },
            { name: 'test_module', type: 'string', required: true, desc: '测试模块名称' },
            { name: 'test_case', type: 'string', required: false, desc: '具体测试用例(可选)' },
            { name: 'retry_dir', type: 'string', required: false, desc: '重试目录(可选)' },
            { name: 'test_suite', type: 'string', required: false, desc: '测试套件路径(可选)' }
        ],
        response: '{ "success": true, "message": "测试已启动" }',
        usage: '启动GMS测试(CTS/GSI/GTS/STS/VTS/APTS)'
    },
    '/api/test/stop': {
        title: '停止测试',
        description: '停止测试',
        params: [],
        response: '{ "success": true, "message": "测试已停止" }',
        usage: '停止测试'
    },
    '/api/test/suites': {
        title: '列出测试套件',
        description: '列出可用的测试套件',
        method: 'GET',
        params: [],
        response: '{ "success": true, "suites": [{"test_type": "cts", "version": "android-cts-16_r4", "tools_path": "...", "full_path": "...", "binary": "cts-tradefed"}], "count": 9, "base_path": "/home/hcq/GMS-Suite" }',
        usage: 'gms-rt-test-suites'
    },
    '/api/test/suites/result': {
        title: '查询测试套件结果',
        description: '查询指定测试套件的测试结果（Tradefed list results命令）',
        method: 'POST',
        params: [
            { name: 'suite_path', type: 'string', required: true, desc: '测试套件tools目录路径，如 /home/hcq/GMS-Suite/android-cts-16.1_r2-1/android-cts/tools' },
            { name: 'tradefed_bin', type: 'string', required: false, desc: 'Tradefed二进制文件名（可选，自动检测）' }
        ],
        response: '{ "success": true, "results": [{"session": "0", "pass": "93608", "fail": "22", "modules": "128 of 131", "result_directory": "2026.04.04_17.05.42", "test_plan": "cts", "device_serial": "RK3572GMS1", "build_id": "BP4A.251205.006", "product": "rk3572_a16"}], "count": 18, "raw_output": "Session...", "cached": false, "query_time": 5.4 }',
        usage: 'gms-rt-test-suites-result ~/GMS-Suite/android-cts-16.1_r2-1/android-cts/tools',
        curl_example: 'curl -X POST "http://172.16.14.233:5001/api/test/suites/result" -H "Content-Type: application/json" -d \'{"suite_path": "/home/hcq/GMS-Suite/android-cts-16.1_r2-1/android-cts/tools"}\''
    },
    '/api/test/clean': {
        title: '清理测试环境',
        description: '清理测试环境',
        params: [],
        response: '{ "success": true, "message": "测试环境已清理" }',
        usage: '清理测试临时文件'
    },
    '/api/test/logs/save': {
        title: '保存当前日志',
        description: '保存当前正在运行的日志',
        params: [],
        response: '{ "success": true, "log_path": "/logs/saved_20260326_110000.log" }',
        usage: '测试运行中保存当前日志快照'
    },
    '/api/test/logs/stream': {
        title: '实时流式日志',
        description: '实时流式输出测试日志',
        method: 'GET',
        params: [],
        response: '实时文本流',
        usage: '实时查看测试日志输出'
    },
    '/api/test/status': {
        title: '获取测试状态',
        description: '获取当前测试运行状态',
        params: [],
        response: '{ "running": false, "test_type": "CTS", "devices": ["RF8TC2W4JNH"] }',
        usage: '查看测试运行状态'
    },
    '/api/system/health': {
        title: '系统管理',
        description: '检查服务器运行状态',
        params: [],
        response: '{ "status": "healthy", "timestamp": "2026-03-26T10:30:00" }',
        usage: '监控服务器健康状态'
    },
    '/api/system/websocket/{client_id}': {
        title: 'WebSocket连接',
        description: '建立WebSocket连接用于实时通信',
        method: 'WebSocket',
        params: [
            { name: 'client_id', type: 'string', required: true, desc: '客户端ID' }
        ],
        response: 'WebSocket连接',
        usage: '实时通信'
    },
    '/api/system/skills': {
        title: '下载技能包',
        description: '下载技能ZIP压缩包（默认下载gms-remote-test技能包）',
        method: 'GET',
        params: [],
        response: 'ZIP文件下载',
        usage: '下载技能包用于离线部署或备份',
        curl_example: 'curl -s -OJ "http://172.16.14.233:5001/api/system/skills"'
    },
    '/api/system/docs': {
        title: '获取API文档',
        description: '获取系统API文档列表',
        method: 'GET',
        params: [],
        response: '{ "apis": [...] }',
        usage: '查看所有可用API'
    },
    '/api/system/help': {
        title: '获取API帮助',
        description: '获取API帮助信息（纯文本格式）',
        method: 'GET',
        params: [],
        response: 'GMS Auto Test API List\n\nTotal: 72 APIs...',
        usage: '查看API列表和使用示例'
    },
    '/api/config/values': {
        title: '获取前端配置',
        description: '获取前端页面需要的配置（不含敏感信息）',
        method: 'GET',
        params: [],
        response: '{ "success": true, "data": {"script_path": "...", "ubuntu_user": "..."}}',
        usage: '获取前端配置信息'
    },
    '/api/config/read': {
        title: '获取完整配置',
        description: '获取完整系统配置（包含所有字段和敏感信息）',
        method: 'GET',
        params: [],
        response: '{ "ubuntu_user": "hcq", "ubuntu_host": "172.16.14.233", "ubuntu_pswd": "..."}',
        usage: '查看完整配置信息'
    },
    '/api/config/update': {
        title: '更新配置',
        description: '更新系统配置（仅修改动态配置字段）',
        method: 'POST',
        params: [
            { name: 'local_server', type: 'string', required: false, desc: '本地服务器地址' },
            { name: 'client_hosts', type: 'object', required: false, desc: '客户端主机映射 {ip: username}' },
            { name: 'client_ssh_credentials', type: 'array', required: false, desc: '客户端SSH凭证列表' }
        ],
        response: '{ "success": true }',
        usage: '修改动态配置字段'
    },
    '/api/users/current': {
        title: '获取客户端信息',
        description: '获取客户端信息',
        params: [],
        response: '{ "ip": "172.16.14.248", "client_id": "hcq@172.16.14.248", "username": "hcq" }',
        usage: '获取客户端身份信息'
    },
    '/api/users/detect': {
        title: '检测客户端信息',
        description: '检测客户端信息',
        params: [
            { name: 'ip', type: 'string', required: false, desc: '客户端IP地址(可选)' },
            { name: 'username', type: 'string', required: false, desc: '用户名(可选)' },
            { name: 'password', type: 'string', required: false, desc: '密码(可选)' }
        ],
        response: '{ "success": true, "username": "hcq" }',
        usage: '自动识别当前用户'
    },
    '/api/users/set-username': {
        title: '设置客户端用户名',
        description: '手动设置客户端用户名（无需SSH密码）',
        params: [
            { name: 'username', type: 'string', required: true, desc: '用户名（不能为unknown）' },
            { name: 'ip', type: 'string', required: false, desc: '客户端IP地址（可选，默认自动获取）' }
        ],
        response: '{ "success": true, "username": "hjf", "ip": "10.10.10.206", "client_id": "hjf@10.10.10.206" }',
        usage: '手动设置用户名'
    },
    '/api/users/list': {
        title: '获取在线用户',
        description: '获取所有在线用户列表',
        params: [],
        response: '{ "users": [{ "client_id": "xxx", "username": "admin", "running": false }] }',
        usage: '查看当前在线用户及其设备使用情况'
    },
    '/api/devices/list': {
        title: '获取设备列表',
        description: '获取Android设备列表',
        params: [
            { name: 'force_refresh', type: 'number', required: false, desc: '是否强制刷新,默认0' }
        ],
        response: '[{ "device_id": "RF8TC2W4JNH", "serial": "RF8TC2W4JNH", "status": "device" }]',
        usage: '查看可用设备列表'
    },
    '/api/devices/bootloader-lock': {
        title: '锁定Bootloader',
        description: '锁定设备Bootloader',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "results": [{ "device": "RF8TC2W4JNH", "success": true }] }',
        usage: '锁定设备Bootloader'
    },
    '/api/devices/bootloader-unlock': {
        title: '解锁Bootloader',
        description: '解锁设备Bootloader',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "results": [{ "device": "RF8TC2W4JNH", "success": true }] }',
        usage: '解锁设备Bootloader'
    },
    '/api/devices/bootloader-status': {
        title: '检查Bootloader锁状态',
        description: '检查设备的Verified Boot锁定状态(GREEN=锁定, ORANGE=未锁定)',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '[{ "device": "RF8TC2W4JNH", "locked": true, "state": "GREEN", "status": "已锁定" }]',
        usage: '检查Bootloader锁定状态'
    },
    '/api/devices/info': {
        title: '获取设备详细信息',
        description: '获取设备的详细硬件和软件信息',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "serial": "RF8TC2W4JNH", "product": "takku", "android_version": "14" }',
        usage: '查看设备详细信息'
    },
    '/api/devices/management': {
        title: '设备管理信息',
        description: '获取所有设备的详细管理信息(设备列表、电池、来源等)',
        params: [],
        response: '[{ "device_id": "xxx", "serial_no": "xxx", "model": "xxx", "android_version": "14", "battery_level": "85", "source_type": "usbip", "source_host": "172.16.14.68", "status": "online", "locked_by": "", "locked_by_self": false }]',
        usage: '查看设备管理信息'
    },
    '/api/devices/user-locked': {
        title: '列出用户锁定设备',
        description: '列出用户锁定设备',
        params: [],
        response: '{ "success": true, "data": { "RF8TC2W4JNH": { "client_id": "hcq@172.16.14.68", "username": "hcq", "timestamp": "2026-04-04T15:30:00" } } }',
        usage: '查看设备占用状态'
    },
    '/api/devices/reboot': {
        title: '重启设备',
        description: '重启设备',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "message": "设备正在重启" }',
        usage: '设备无响应或需要清理状态时重启'
    },
    '/api/devices/remount': {
        title: '重新挂载设备',
        description: '将设备重新挂载为读写模式',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "message": "设备已重新挂载为读写模式" }',
        usage: '需要修改系统文件时使用'
    },
    '/api/devices/wifi-connect': {
        title: '连接WiFi',
        description: '让设备连接到指定的WiFi网络',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' },
            { name: 'ssid', type: 'string', required: false, desc: 'WiFi名称，默认AndroidWifi' },
            { name: 'password', type: 'string', required: false, desc: 'WiFi密码，默认1234567890' }
        ],
        response: '{ "success": true, "message": "WiFi连接成功" }',
        usage: '配置设备连接到WiFi网络'
    },
    '/api/devices/shell': {
        title: '执行Shell命令',
        description: '在设备上执行ADB Shell命令',
        params: [
            { name: 'serial_no', type: 'string', required: true, desc: '设备序列号' }
        ],
        response: '{ "success": true, "output": "命令输出..." }',
        usage: '建立ADB Shell会话'
    },
    '/api/devices/screen': {
        title: '显示设备屏幕',
        description: '启动设备屏幕显示(VNC)',
        params: [
            { name: 'devices', type: 'array', required: true, desc: '设备序列号数组' }
        ],
        response: '{ "success": true, "screens": [{ "device_id": "RF8TC2W4JNH", "port": 5900 }] }',
        usage: '批量查看设备屏幕'
    },
    '/api/reports/list': {
        title: '获取报告列表',
        description: '获取所有历史测试报告',
        params: [],
        response: '{ "reports": [{ "timestamp": "20260326_100000", "test_type": "CTS" }] }',
        usage: '查看所有历史测试报告'
    },
    '/api/reports/analyze/{report_timestamp}': {
        title: '分析报告',
        description: '分析测试报告',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳' }
        ],
        response: '{ "summary": { "passed": 150, "failed": 5 }, "failed_tests": [] }',
        usage: '快速查看测试结果统计和失败用例'
    },
    '/api/reports/download': {
        title: '获取报告',
        description: '获取测试报告（查看或下载）',
        params: [
            { name: 'path', type: 'string', required: false, desc: '报告文件路径' },
            { name: 'report_timestamp', type: 'string', required: false, desc: '报告时间戳（用于下载）' }
        ],
        response: '报告内容或ZIP文件',
        usage: '查看或下载测试报告'
    },
    '/api/reports/delete': {
        title: '删除报告',
        description: '删除指定的测试报告',
        params: [
            { name: 'report_timestamp', type: 'string', required: true, desc: '报告时间戳（如20260330-120000）' }
        ],
        response: '{ "success": true, "message": "报告已删除" }',
        usage: '删除测试报告',
        curl_example: 'curl -X DELETE "http://server:5001/api/reports/delete" -G -d "report_timestamp=20260330-120000"'
    },
    '/api/desktop/vnc/status': {
        title: '获取桌面VNC状态',
        description: '检查桌面VNC服务运行状态',
        params: [],
        response: '{ "running": false, "port": 5900 }',
        usage: '检查VNC服务是否正在运行'
    },
    '/api/desktop/vnc/start': {
        title: '启动桌面VNC',
        description: '启动桌面VNC服务',
        params: [
            { name: 'host', type: 'string', required: false, desc: '主机地址 (user@ip)' },
            { name: 'password', type: 'string', required: false, desc: 'SSH密码' },
            { name: 'vnc_password', type: 'string', required: false, desc: 'VNC密码' }
        ],
        response: '{ "success": true, "port": 5900, "url": "..." }',
        usage: 'gms-rt-desktop-vnc-start'
    },
    '/api/desktop/vnc/stop': {
        title: '停止桌面VNC',
        description: '停止桌面VNC服务',
        params: [],
        response: '{ "success": true, "message": "VNC已停止" }',
        usage: '停止VNC服务'
    },
    '/api/desktop/validate': {
        title: '验证Ubuntu主机桌面',
        description: '验证Ubuntu主机SSH连接并检查VNC服务可用性',
        params: [
            { name: 'host', type: 'string', required: true, desc: '主机地址（格式：user@ip，如hcq@172.16.14.233）' },
            { name: 'password', type: 'string', required: false, desc: 'SSH登录密码（可选）' }
        ],
        response: '{ "success": true, "message": "SSH连接成功，VNC服务可用" }',
        usage: '验证SSH和VNC连接'
    },
    '/api/desktop/vnc/status': {
        title: '查询Ubuntu主机桌面VNC状态',
        description: '查询Ubuntu主机桌面VNC服务状态',
        params: [],
        response: '{ "success": true, "running": true, "url": "http://172.16.14.233:6080/vnc.html" }',
        usage: '检查VNC服务运行状态'
    },
    '/api/desktop/vnc/start': {
        title: '启动Ubuntu主机桌面VNC',
        description: '启动Ubuntu主机桌面VNC服务',
        params: [
            { name: 'host', type: 'string', required: false, desc: 'Ubuntu主机桌面地址，格式：user@ip' },
            { name: 'password', type: 'string', required: false, desc: 'SSH登录密码' },
            { name: 'vnc_password', type: 'string', required: false, desc: 'VNC访问密码（可选）' }
        ],
        response: '{ "success": true, "url": "http://172.16.14.233:6080/vnc.html" }',
        usage: '启动Ubuntu主机桌面VNC服务'
    },
    '/api/desktop/vnc/stop': {
        title: '停止Ubuntu主机桌面VNC',
        description: '停止Ubuntu主机桌面VNC服务',
        params: [],
        response: '{ "success": true, "message": "Ubuntu主机桌面VNC已停止" }',
        usage: '停止Ubuntu主机桌面VNC服务'
    },
    '/api/adb-forward/start': {
        title: '启动ADB端口转发',
        description: '启动ADB端口转发',
        params: [
            { name: 'device_host', type: 'string', required: true, desc: '设备主机地址' },
            { name: 'device_password', type: 'string', required: true, desc: '设备SSH密码' }
        ],
        response: '{ "success": true, "forwarding": [] }',
        usage: '启动ADB端口转发'
    },
    '/api/adb-forward/stop': {
        title: '停止ADB端口转发',
        description: '停止ADB端口转发',
        params: [
            { name: 'device_id', type: 'string', required: true, desc: '设备序列号' }
        ],
        response: '{ "success": true, "message": "ADB端口转发已停止" }',
        usage: '停止ADB端口转发'
    },
    '/api/usbip/status': {
        title: '获取USB/IP状态',
        description: '检查USB/IP服务状态',
        params: [],
        response: '{ "installed": true, "running": false }',
        usage: '检查USB/IP服务状态'
    },
    '/api/usbip/connect': {
        title: '启动USB/IP',
        description: '启动USB/IP设备共享',
        params: [
            { name: 'device_host', type: 'string', required: false, desc: '设备主机地址，如172.16.14.233' },
            { name: 'device_password', type: 'string', required: false, desc: '设备主机SSH密码（可选）' }
        ],
        response: '{ "success": true, "message": "USB/IP已启动" }',
        usage: '启动USB/IP服务'
    },
    '/api/usbip/disconnect': {
        title: '停止USB/IP',
        description: '停止USB/IP服务',
        params: [],
        response: '{ "success": true, "message": "USB/IP已停止" }',
        usage: '停止USB/IP服务'
    },
    '/api/usbip/auto-install': {
        title: '自动安装USB/IP',
        description: '自动安装USB/IP服务',
        params: [],
        response: '{ "success": true, "message": "USB/IP已自动安装" }',
        usage: '一键安装USB/IP服务'
    },
    '/api/ssh/sshd-check': {
        title: '检查SSHD状态',
        description: '检查SSH服务状态',
        params: [],
        response: '{ "installed": true, "running": true }',
        usage: '检查SSH服务是否正常运行'
    },
    '/api/ssh/sshd-install': {
        title: '安装SSHD',
        description: '获取SSHD安装指南',
        params: [],
        response: '{ "success": false, "error": "SSHD需要在Windows客户端手动安装", "install_guide": "安装步骤...", "manual_install": true }',
        usage: '安装Windows SSHD服务'
    },
    '/api/ssh/ping': {
        title: 'SSH连通性测试',
        description: '测试客户端到测试主机的网络连通性',
        params: [
            { name: 'test_host_ip', type: 'string', required: true, desc: '测试主机IP' },
            { name: 'client_ip', type: 'string', required: true, desc: '客户端IP' }
        ],
        response: '{ "success": true, "reachable": true, "latency": "<1ms", "route_commands": {...} }',
        usage: '检查主机间网络连通性'
    },
    '/api/ssh/route': {
        title: '检查路由',
        description: '检查系统路由表',
        params: [],
        response: '{ "routing_table": [] }',
        usage: '查看系统路由配置'
    },
    '/api/vpn/status': {
        title: '获取VPN状态',
        description: '检查VPN连接状态',
        params: [],
        response: '{ "success": true, "connected": true }',
        usage: '检查VPN是否已连接'
    },
    '/api/vpn/connect': {
        title: '连接VPN',
        description: '连接到默认VPN服务器（无需参数）',
        params: [],
        response: '{ "success": true, "message": "VPN已连接" }',
        usage: '连接到默认VPN服务器'
    },
    '/api/vpn/disconnect': {
        title: '断开VPN',
        description: '断开VPN连接',
        params: [],
        response: '{ "success": true, "message": "VPN已断开" }',
        usage: '断开当前VPN连接'
    },
    '/api/files/install': {
        title: '上传并安装',
        description: '上传APK并安装到设备',
        method: 'POST',
        params: [
            { name: 'file', type: 'file', required: true, desc: 'APK文件' },
            { name: 'device_id', type: 'string', required: true, desc: '目标设备序列号' }
        ],
        response: '{ "success": true, "message": "应用已安装" }',
        usage: '上传并安装APK到指定设备'
    },
    '/api/files/progress': {
        title: '获取上传进度',
        description: '获取当前文件上传进度',
        method: 'GET',
        params: [
            { name: 'upload_id', type: 'string', required: false, desc: '上传任务ID' }
        ],
        response: '{ "uploading": false, "progress": 0 }',
        usage: '查看文件上传进度'
    },
    '/api/burn/firmware': {
        title: '烧写固件',
        description: '烧写固件',
        params: [
            { name: 'firmware_file', type: 'file', required: true, desc: '固件文件（.img格式）' },
            { name: 'devices', type: 'string', required: true, desc: '设备序列号（多个用逗号分隔）' },
            { name: 'wipe_data', type: 'boolean', required: false, desc: '是否清除数据（默认true）' }
        ],
        response: '{ "success": true, "message": "固件烧写成功" }',
        usage: '烧写固件',
        curl_example: 'curl -X POST "http://server:5001/api/burn/firmware" -F "devices=rk3572cai" -F "firmware_file=@/path/to/firmware.img" -F "wipe_data=true"'
    },
    '/api/burn/gsi': {
        title: '烧写GSI',
        description: '烧写GSI',
        params: [
            { name: 'gsi_image', type: 'file', required: true, desc: 'GSI镜像文件（.img格式）' },
            { name: 'devices', type: 'string', required: true, desc: '设备序列号（多个用逗号分隔）' },
            { name: 'wipe_data', type: 'boolean', required: false, desc: '是否清除数据（默认true）' }
        ],
        response: '{ "success": true, "message": "GSI烧写成功" }',
        usage: '⚠️危险操作 - 烧写GSI镜像'
    },
    '/api/burn/serial': {
        title: '烧写设备序列号',
        description: '烧写设备序列号',
        params: [
            { name: 'device_id', type: 'string', required: true, desc: '当前设备序列号' },
            { name: 'new_serial', type: 'string', required: true, desc: '新的序列号' }
        ],
        response: '{ "success": true, "message": "序列号已修改" }',
        usage: '修改设备序列号'
    },
    '/api/burn/upload-progress': {
        title: '固件上传进度',
        description: '查询固件上传进度',
        method: 'GET',
        params: [],
        response: '{ "in_progress": true, "progress": 45.5, "filename": "update.img" }',
        usage: '查看固件上传进度'
    },
    '/api/files/list': {
        title: '列出文件',
        description: '列出设备指定目录的文件',
        params: [
            { name: 'path', type: 'string', required: true, desc: '目录路径,如/sdcard' }
        ],
        response: '{ "files": [{ "name": "DCIM", "type": "directory" }] }',
        usage: '浏览设备文件系统'
    },
    '/api/terminal/push': {
        title: '推送文件到主机终端',
        description: '上传文件到测试主机的指定目录（默认 /home/hcq/GMS-Suite/tmp，支持分块上传和断点续传）',
        method: 'POST',
        params: [
            { name: 'file', type: 'file', required: true, desc: '要上传的文件' },
            { name: 'path', type: 'string', required: false, desc: '目标路径，默认 /home/hcq/GMS-Suite/tmp' },
            { name: 'chunk_index', type: 'int', required: false, desc: '分块索引（分块上传时使用）' },
            { name: 'total_chunks', type: 'int', required: false, desc: '总分块数（分块上传时使用）' },
            { name: 'upload_id', type: 'string', required: false, desc: '上传任务ID（分块上传时使用）' }
        ],
        response: '{ "success": true, "remote_path": "/home/hcq/GMS-Suite/tmp/filename.ext", "message": "文件已上传到 /home/hcq/GMS-Suite/tmp/filename.ext" }',
        usage: 'gms-rt-terminal-push filename.ext',
        curl_example: 'curl -X POST "http://172.16.14.233:5001/api/terminal/push" -F "file=@localfile.txt" -F "path=/home/hcq/GMS-Suite/tmp"'
    },
    '/api/terminal/open': {
        title: '打开主机终端',
        description: '获取SSH终端连接信息，用于建立SSH连接到测试主机',
        method: 'GET',
        params: [],
        response: '{ "success": true, "host": "172.16.14.233", "user": "hcq", "port": 22, "connection_command": "ssh hcq@172.16.14.233", "instructions": ["1. 复制连接命令: ssh hcq@172.16.14.233", "2. 在终端中粘贴并执行连接命令", "3. 输入密码或使用SSH密钥认证", "4. 连接成功后，您将获得测试主机的终端访问权限"] }',
        usage: 'gms-rt-terminal-open',
        curl_example: 'curl -s "http://172.16.14.233:5001/api/terminal/open" | jq \'.connection_command\''
    },
    '/api/opengrok/search': {
        title: 'OpenGrok搜索',
        description: '在源码中搜索代码',
        params: [
            { name: 'query', type: 'string', required: true, desc: '搜索关键词' },
            { name: 'full', type: 'boolean', required: false, desc: '是否全文搜索' }
        ],
        response: '{ "results": [{ "file": "/path/to/Test.java", "line": 10 }] }',
        usage: '搜索Android源码'
    }
};
