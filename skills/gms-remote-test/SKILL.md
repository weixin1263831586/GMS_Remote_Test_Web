---
name: gms-remote-test
version: "2026.04.24-3.2.0"
description: |
  【gms-remote-test】GMS Remote Test Web Platform & API Skill for FastAPI (Port 5001).
  Remote Android device testing, CTS/VTS/GTS execution, device management.

  当用户询问"有哪些技能"时，请阅读本文件完整内容，按以下格式展示所有技能：

  ## 技能列表 (共 14 类，54 个 API + 2 个 Web API)

  请按顺序展示以下所有分类的完整表格：

  **测试执行 (7 个)**、**主机桌面 (4 个)**、**主机终端 (2 个)**、**设备管理 (11 个)**、**用户管理 (4 个)**、**报告管理 (4 个)**、**固件烧写 (3 个)**、**文件管理 (2 个)**、**配置管理 (2 个)**、**系统管理 (4 个)**、**SSH 管理 (4 个)**、**VPN 管理 (3 个)**、**USB/IP 管理 (6 个)**、**其他 (1 个)**

  每个表格包含三列：API 端点、CLI 命令、功能说明。所有分类都必须完整显示，不要省略。

  展示完所有技能表格后，还要显示"技能使用说明"部分，包括：使用方式（CLI 和 API 调用）、参数格式说明、常用场景示例、响应格式、环境变量配置。
---

# GMS Remote Test Platform - 完整技能文档

## 技能列表

### 测试执行 (7 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `POST /api/test/start` | `gms-rt-test-start` | Start a test execution |
| `POST /api/test/stop` | `gms-rt-test-stop` | Stop running test |
| `GET /api/test/status` | `gms-rt-test-status` | Check test status |
| `GET /api/test/suites` | `gms-rt-test-suites` | List available test suites |
| `POST /api/test/suites/result` | `gms-rt-test-suites-result` | Parse test suites result |
| `GET /api/test/logs/stream` | `gms-rt-test-logs-stream` | Stream logs in real-time |
| `POST /api/test/clean` | `gms-rt-test-clean` | Clean test environment |

### 主机桌面 (4 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `POST /api/desktop/vnc/start` | `gms-rt-desktop-vnc-start` | Start VNC session |
| `POST /api/desktop/vnc/stop` | `gms-rt-desktop-vnc-stop` | Stop VNC session |
| `GET /api/desktop/vnc/status` | `gms-rt-desktop-vnc-status` | Check VNC status |
| `POST /api/desktop/validate` | `gms-rt-desktop-validate` | Validate desktop host |

### 主机终端 (2 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `GET /api/terminal/open` | `gms-rt-terminal-open` | Open terminal connection |
| `POST /api/terminal/push` | `gms-rt-terminal-push` | Push command to terminal |

### 设备管理 (11 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `GET /api/devices/list` | `gms-rt-devices-list` | List all connected devices |
| `POST /api/devices/info` | `gms-rt-devices-info` | Get detailed device information |
| `POST /api/devices/reboot` | `gms-rt-devices-reboot` | Reboot devices |
| `POST /api/devices/remount` | `gms-rt-devices-remount` | Remount filesystem RW |
| `POST /api/devices/bootloader-lock` | `gms-rt-devices-bootloader-lock` | Lock bootloader |
| `POST /api/devices/bootloader-unlock` | `gms-rt-devices-bootloader-unlock` | Unlock bootloader |
| `POST /api/devices/bootloader-status` | `gms-rt-devices-bootloader-status` | Check bootloader status |
| `POST /api/devices/wifi` | `gms-rt-devices-wifi` | Connect to WiFi |
| `POST /api/devices/scrcpy` | `gms-rt-devices-scrcpy` | Show device screen |
| `POST /api/devices/shell` | `gms-rt-devices-shell` | Execute ADB shell command |
| `GET /api/devices/user-locked` | `gms-rt-devices-user-locked` | List user locks |

### 用户管理 (4 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `GET /api/users/current` | `gms-rt-users-current` | Get current user info |
| `POST /api/users/detect` | `gms-rt-users-detect` | Auto-detect username |
| `POST /api/users/set-username` | `gms-rt-users-set-username` | Set username manually |
| `GET /api/users/list` | `gms-rt-users-list` | List all users |

### 报告管理 (4 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `GET /api/reports/list` | `gms-rt-reports-list` | List all test reports |
| `GET /api/reports/download` | `gms-rt-reports-download` | Get report |
| `DELETE /api/reports/delete` | `gms-rt-reports-delete` | Delete report |
| `POST /api/reports/analyze` | `gms-rt-reports-analyze` | Analyze saved report |

### 固件烧写 (3 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `POST /api/burn/firmware` | `gms-rt-burn-firmware` | Burn firmware image |
| `POST /api/burn/gsi` | `gms-rt-burn-gsi` | Burn GSI image |
| `POST /api/burn/serial` | `gms-rt-burn-serial` | Burn serial number |

### 文件管理 (2 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `GET /api/files/progress` | `gms-rt-files-progress` | Get file upload progress |
| `POST /api/files/list` | *(Web API)* | List files |

### 配置管理 (2 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `GET /api/config/read` | `gms-rt-config-read` | Read configuration |
| `POST /api/config/update` | `gms-rt-config-update` | Update configuration |

### 系统管理 (4 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `GET /api/system/health` | `gms-rt-system-health` | Health check |
| `GET /api/system/docs` | `gms-rt-system-docs` | API documentation |
| `GET /api/system/help` | `gms-rt-system-help` | API help |
| `GET /api/system/skills` | `gms-rt-system-skills` | Download skills ZIP |

### SSH 管理 (5 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `POST /api/ssh/ping` | `gms-rt-ssh-ping` | Test connectivity |
| `GET /api/ssh/route` | `gms-rt-ssh-route` | Check SSH routing |
| `GET /api/ssh/sshd` | `gms-rt-ssh-sshd` | Check SSHD status & install guide |

### VPN管理 (3 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `POST /api/vpn/connect` | `gms-rt-vpn-connect` | Connect to VPN |
| `POST /api/vpn/disconnect` | `gms-rt-vpn-disconnect` | Disconnect VPN |
| `GET /api/vpn/status` | `gms-rt-vpn-status` | Check VPN status |

### USB/IP 管理 (6 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `POST /api/usbip/connect` | `gms-rt-usbip-connect` | Start USB/IP connection |
| `POST /api/usbip/disconnect` | `gms-rt-usbip-disconnect` | Stop USB/IP connection |
| `GET /api/usbip/status` | `gms-rt-usbip-status` | Check USB/IP status |
| `POST /api/usbip/install` | `gms-rt-usbip-install` | Install USB/IP |
| `POST /api/adb-forward/start` | `gms-rt-adb-forward-start` | Start ADB port forwarding |
| `POST /api/adb-forward/stop` | `gms-rt-adb-forward-stop` | Stop ADB port forwarding |

### 其他 (1 个)
| API | CLI 命令 | 功能 |
|-----|----------|------|
| `POST /api/opengrok/search` | `gms-rt-opengrok-search` | Search OpenGrok code |

---

## 技能使用说明

### 使用方式

本技能支持两种调用方式：

**1. CLI 命令调用（推荐）**
```bash
# 基本用法
gms-rt-devices-list
gms-rt-test-status
gms-rt-devices-reboot "device1 device2"

# 带参数调用
gms-rt-devices-shell "device1" "ls -la"
gms-rt-test-start "CTS_DEX" "--device device1"
```

**2. API 直接调用**
```bash
# GET 请求
curl http://172.16.14.233:5001/api/devices/list

# POST 请求
curl -X POST http://172.16.14.233:5001/api/devices/reboot \
  -H "Content-Type: application/json" \
  -d '{"devices": ["device1", "device2"]}'
```

### 参数格式说明

| 参数类型 | 格式示例 | 说明 |
|----------|----------|------|
| 设备列表 | `"device1 device2"` | 空格分隔的设备 ID 字符串 |
| 设备列表 (JSON) | `["device1","device2"]` | JSON 数组格式 |
| 测试套件 | `"CTS_DEX"` | 测试套件名称 |
| 文件路径 | `"/path/to/file.zip"` | 服务器上的绝对路径 |
| 命令 | `"ls -la"` | ADB shell 命令 |

### 常用场景示例

**查看设备列表**
```bash
gms-rt-devices-list
```

**重启指定设备**
```bash
gms-rt-devices-reboot "R58M1234567"
```

**执行测试**
```bash
gms-rt-test-start "CTS_DEX" '{"devices": ["R58M1234567"], "repeat": 1}'
```

**查看测试状态**
```bash
gms-rt-test-status
```

**实时查看日志**
```bash
gms-rt-test-logs-stream
```

**设备解锁 Bootloader**
```bash
gms-rt-devices-bootloader-unlock "R58M1234567"
```

### 响应格式

所有 API 返回 JSON 格式：
```json
{
  "success": true,
  "message": "Operation successful",
  "data": { ... }
}
```

错误响应：
```json
{
  "success": false,
  "error": "Error message",
  "detail": "Detailed error information"
}
```

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `GMS_REMOTE_TEST_SERVER` | 服务器地址 | `http://172.16.14.233:5001` |
| `GMS_WEB_APP_DIR` | Web 应用目录 | `/home/hcq/GMS_Remote_Test/web_app` |

---

# GMS Remote Test Platform

**Quick Start**:
```bash
bash ~/.claude/skills/gms-remote-test/scripts/gms-remote-test.sh gms-rt-devices-list
```

**Web Interface**: http://172.16.14.233:5001
**API Docs**: http://172.16.14.233:5001/docs

## Platform Overview

GMS Remote Test Platform provides comprehensive Android device testing capabilities through both web interface and REST API. Supports CTS/VTS/GTS test execution, device management (local/USB/IP), firmware burning, report analysis, and multi-user collaboration.

Key features:
- Parallel device operations (10 devices in 10-15s vs 60-90s)
- Real-time log streaming via WebSocket
- VNC desktop access for remote GUI control
- Multi-user device locking mechanism
- Advanced firmware/GSI/serial number burning

## Error Handling

All endpoints return JSON with `success` field. Check `success === true` for successful operations. Error messages are in `message`, `detail`, or `error` fields.

**Common Issues**:
- **Device operations timeout**: Device may be offline; check with `/api/devices/list`
- **Test not starting**: Verify device is unlocked and not in use by another user
- **Connection refused**: Check network connectivity and firewall rules (ports 5001, 22, 6080)
