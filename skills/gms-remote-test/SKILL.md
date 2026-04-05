---
name: gms-remote-test
version: "2026.04.05-200000"
description: >-
  GMS Remote Test Web Platform & API Skill for FastAPI (Port 5001).
  **Complete web interface** with device management, test execution, desktop VNC, terminal access,
  user management, device locking, report analysis, and route diagnostics.
  **Latest**: Route check terminal with auto-execute commands, O(n) buffer optimization, memory leak fixes.
---

# GMS Remote Test Platform - Complete Guide

**Interactive Web Platform**: http://172.16.14.233:5001 (full-featured web interface)
**API Endpoints**: RESTful API for automation and scripting
**Best for**: Remote Android device testing, CTS/VTS/GTS execution, device management, and test automation

## Quick Reference

| Item | Value |
|------|-------|
| **Web Interface** | http://172.16.14.233:5001 |
| **API Docs (Swagger)** | http://172.16.14.233:5001/docs |
| **API Help** | http://172.16.14.233:5001/api/help |
| **Skill Version** | `2026.04.05-200000` |
| **Performance** | 75-85% faster multi-device operations (parallel execution) |

---

## Platform Overview

### Web Interface Features

The web platform provides **8 integrated pages** for complete test management:

1. **📱 测试界面 (Test Interface)** - Main test control panel
   - Device selection and management
   - Test parameter configuration
   - Real-time log streaming
   - File upload to test host
   - VPN/USB/IP connectivity

2. **🖥️ 主机桌面 (Desktop VNC)** - Remote desktop access
   - VNC viewer for test host desktop
   - Multi-host support
   - Host validation and management

3. **🐧 主机终端 (Terminal)** - SSH terminal access
   - Full xterm.js terminal emulator
   - WebSocket-based real-time connection
   - Drag-and-drop file upload
   - Auto-execute route commands

4. **📱 设备管理 (Device Management)** - Device inventory
   - Device list with details (model, Android version, battery)
   - Sortable columns
   - Lock/unlock status tracking
   - Source type identification (local/USB/IP)

5. **👥 用户管理 (User Management)** - Multi-user support
   - Online user monitoring
   - Active user tracking
   - Device allocation per user
   - User activity statistics

6. **📊 报告管理 (Report Management)** - Test report library
   - Report listing with pass/fail statistics
   - Per-user report filtering
   - Report download and analysis

7. **📈 报告分析 (Report Analysis)** - Report analysis tool
   - Drag-and-drop report upload
   - XML/ZIP/TAR.GZ support
   - Failure case extraction
   - Statistics summary

8. **📡 系统API (System API)** - Interactive API documentation
   - Complete API endpoint reference
   - Category filtering (test, device, desktop, VPN, etc.)
   - Copy-to-clipboard functionality
   - Usage examples

---

## What's New (2026.04.05)

### Latest Features
- ✅ **Route Check Terminal**: Auto-execute route commands with cross-page navigation
- ✅ **Performance Optimization**: O(n) buffer handling (was O(n²))
- ✅ **Memory Leak Fix**: Proper cleanup of `last_saved_log_file` entries
- ✅ **Sticky Dialog Headers**: Close button (X) on route check dialog
- ✅ **Terminal Command Auto-fill**: Seamless command input from route check

### Previous Improvements
- ✅ **API Naming**: `/api/config` → `/api/config/read` and `/api/config/update`
- ✅ **Desktop VNC**: Unified endpoints `/api/desktop/vnc/*`
- ✅ **Parallel Operations**: 75-85% faster multi-device execution
- ✅ **Client Tracking**: Improved client information management

---

## Web Interface Features

### Test Interface (测试界面)

#### Device Operations
- **🔄 刷新设备** - Refresh connected ADB devices
- **✅ 全选设备** - Select all devices for batch operations
- **⏻ 重启设备** - Reboot selected devices (parallel execution)
- **⏻ Remount** - Remount system partition as read-write
- **🛜 连接Wifi** - Configure WiFi on selected devices
- **🔒 锁定设备** - Lock devices for exclusive use
- **🔓 解锁设备** - Unlock devices for other users
- **🔐 锁定状态** - Check device lock status
- **📋 设备信息** - Collect detailed device information (model, Android version, battery, etc.)

#### VNC & Screen Control
- **🚀 启动VNC** - Start VNC server for remote desktop viewing
- **📺 设备投屏** - Show device screen via scrcpy
- **🔌 端口转发** - Setup ADB port forwarding
- **📱 本地设备** - Connect USB/IP devices from Windows host

#### Network & VPN
- **📡 检查SSHD** - Verify SSH server status
- **📡 检查路由** - Test network routing (includes route command dialog)
  - Shows connectivity status between client and test host
  - Provides Linux/Windows route commands
  - **NEW**: "🖥️ 打开主机终端" button auto-switches to terminal with route command pre-filled
- **🔌 连接VPN** - Connect to VPN for remote access
- **📡 检查VPN** - Check VPN connection status

#### File Management
- **📤 上传到测试主机** - Upload files via drag-and-drop or file picker
- **Progress bar** - Real-time upload progress tracking

#### Test Controls
- **▶ 开始测试** - Start CTS/VTS/GTS test execution
- **⏸ 停止测试** - Stop running test
- **📥 保存日志** - Download test log file
- **🧹 清除日志** - Clear test log display
- **⚙️ 配置** - Open system configuration modal

---

### Desktop VNC (主机桌面)

#### Features
- **Multi-host support** - Switch between different test hosts
- **VNC viewer** - Full desktop remote control via web browser
- **Host validation** - Verify host connectivity before connecting
- **Auto-connect** - Automatic VNC connection on page load
- **Password support** - Secure VNC authentication

#### Controls
- **🔄 刷新** - Refresh VNC connection
- **➕ 添加主机** - Add new desktop host to the list

---

### Terminal (主机终端)

#### Features
- **xterm.js terminal** - Full-featured terminal emulator in browser
- **WebSocket connection** - Real-time bidirectional communication
- **SSH integration** - Automatic SSH connection to test host
- **Silent mode** - Buffers output until shell prompt detected
- **Command history** - Navigate previous commands with arrow keys

#### File Upload
- **Drag-and-drop** - Drop files directly onto terminal to upload to `/tmp`
- **Visual feedback** - Overlay shows when drag is active

#### Controls
- **🧹 清空** - Clear terminal screen
- **🔄 重连** - Reconnect to SSH session

#### Route Command Integration
- **Auto-execute** - Route commands from "检查路由" dialog auto-input
- **Cursor positioning** - Cursor placed after command, waiting for Enter
- **Smooth transition** - Seamless navigation from route check to terminal

---

### Device Management (设备管理)

#### Statistics Cards
- **总设备数** - Total connected devices
- **测试主机设备** - Devices directly connected to test host
- **USB/IP设备** - Devices connected via USB/IP

#### Device List Columns
- **设备序列号** - Device serial number (sortable)
- **设备型号** - Device model (sortable)
- **Android版本** - Android version (sortable)
- **电池电量** - Battery level (sortable)
- **状态** - Device status (sortable)
- **来源类型** - Connection type (local/USB/IP) (sortable)
- **来源主机** - Source host (sortable)
- **占用用户** - Lock owner (sortable)
- **操作** - Action buttons

#### Features
- **Click column headers** - Sort by any column
- **Real-time updates** - WebSocket-driven device status updates
- **Lock indicators** - Visual lock status per device

---

### User Management (用户管理)

#### Statistics Cards
- **在线用户数** - Currently connected users
- **活跃用户数** - Active users in last 5 minutes
- **测试中用户** - Users currently running tests

#### User List Columns
- **用户标识** - Client identifier (username@hostname)
- **IP地址** - Client IP address
- **状态** - Connection status
- **连接时间** - Connection timestamp
- **最后活跃** - Last activity time
- **占用设备** - Devices locked by this user

#### Features
- **Real-time monitoring** - Live user activity tracking
- **Auto-refresh** - WebSocket-driven updates
- **Device allocation** - See which user owns which devices

---

### Report Management (报告管理)

#### Features
- **Report listing** - All test reports with timestamps
- **Statistics** - Pass/fail counts and pass rate
- **Per-user filtering** - Show only current user's reports
- **Report download** - Download full report archives
- **Delete reports** - Remove old reports

#### Report Columns
- **客户端** - Client identifier
- **类型** - Test type (CTS/VTS/GTS)
- **时间戳** - Report timestamp
- **通过** - Passed test count
- **失败** - Failed test count
- **总计** - Total test count
- **通过率** - Pass percentage
- **操作** - Action buttons (download/delete)

---

### Report Analysis (报告分析)

#### Features
- **Drag-and-drop upload** - Drop XML/ZIP/TAR.GZ reports
- **Automatic parsing** - Extract test results from XML
- **Statistics summary** - Overall pass/fail statistics
- **Failure list** - Detailed failure case listing
- **Re-run support** - Generate re-run commands for failed cases

#### Upload Options
- **Single file** - Upload .xml report file
- **Archive** - Upload .zip or .tar.gz archives
- **Folder** - Upload entire report folder

#### Analysis Results
- **Summary cards** - Total tests, passed, failed, pass rate
- **Module breakdown** - Results per test module
- **Failure details** - Test name, failure reason
- **Copy to clipboard** - One-click failure list copy

---

### System API (系统API)

#### Features
- **Complete API reference** - All available endpoints
- **Category filtering** - Filter by feature category
- **Search** - Search by path or description
- **Copy to clipboard** - Click command to copy
- **Usage examples** - Practical example workflows

#### API Categories
- **🧪 测试管理** - Test execution and control
- **🖥️ 主机桌面** - Desktop VNC management
- **📱 设备管理** - Device operations
- **👥 用户管理** - User tracking
- **📊 报告管理** - Report operations
- **🔥 固件烧写** - Firmware/GSI/SN burning
- **📁 文件管理** - File operations
- **⚙️ 配置管理** - Configuration management
- **💚 系统管理** - Health and system status
- **🔑 SSH管理** - SSH operations
- **🔐 VPN管理** - VPN control
- **📡 USB/IP** - USB/IP device connection

#### Statistics
- **API总数** - Total endpoint count
- **GET接口** - GET endpoint count
- **POST接口** - POST endpoint count
- **筛选结果** - Filtered result count

---

## Core API Features

### 1. Device Discovery & Management

#### List All Connected Devices
```bash
curl -s http://172.16.14.233:5001/api/devices/list | jq '.'
```

**Response format:**
```json
{
  "devices": [
    {
      "device_id": "RK3588-DEVICE",
      "model": "Rockchip RK3588",
      "state": "device"
    }
  ]
}
```

#### Get Device Info (Parallel Execution)
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/info \
  -H "Content-Type: application/json" \
  -d '{"devices": ["DEVICE-1", "DEVICE-2"]}' | jq '.'
```

**Performance**: 10 devices in 10-15 seconds (vs 60-90 seconds before)

---

### 2. Desktop VNC Management

#### Start Desktop VNC
```bash
curl -sX POST http://172.16.14.233:5001/api/desktop/vnc/start \
  -H "Content-Type: application/json" \
  -d '{
    "host": "172.16.14.233",
    "username": "hcq"
  }' | jq '.'
```

#### Check Desktop VNC Status
```bash
curl -s http://172.16.14.233:5001/api/desktop/vnc/status | jq '.'
```

#### Stop Desktop VNC
```bash
curl -sX POST http://172.16.14.233:5001/api/desktop/vnc/stop | jq '.'
```

#### Validate Desktop Host
```bash
curl -sX POST http://172.16.14.233:5001/api/desktop/validate \
  -H "Content-Type: application/json" \
  -d '{"host": "172.16.14.233"}' | jq '.'
```

---

### 3. USB/IP Remote Connection

#### Start USB/IP Connection
```bash
curl -sX POST http://172.16.14.233:5001/api/usbip/start \
  -H "Content-Type: application/json" \
  -d '{
    "device_host": "username@windows-ip",
    "device_password": "password"
  }' | jq '.'
```

**Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `device_host` | string | Yes | Windows host (format: `user@ip`) |
| `device_password` | string | Yes | SSH password for Windows host |

**Response:**
```json
{
  "success": true,
  "message": "✅ 成功连接 1 个设备：RK3588-DEVICE",
  "devices": ["RK3588-DEVICE"],
  "device_list": ["RK3588-DEVICE"]
}
```

#### Stop USB/IP Connection
```bash
curl -sX POST http://172.16.14.233:5001/api/usbip/stop | jq '.'
```

#### Check USB/IP Status
```bash
curl -s http://172.16.14.233:5001/api/usbip/status | jq '.'
```

---

### 4. Test Execution

#### Start a Test
```bash
curl -sX POST http://172.16.14.233:5001/api/test/start \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RK3588-DEVICE"],
    "test_type": "CTS",
    "test_module": "CtsPermissionTestCases",
    "test_suite": "/home/hcq/GMS-Suite/android-cts-16_r4/android-cts/tools"
  }' | jq '.'
```

**Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `devices` | string[] | Yes | Device serial numbers |
| `test_type` | string | Yes | CTS, VTS, GTS, etc. |
| `test_module` | string | Yes | Test module name |
| `test_suite` | string | No | Path to test suite binary |
| `test_case` | string | No | Specific test case filter |

**Response:**
```json
{
  "success": true,
  "message": "✅ 测试已启动",
  "devices": ["RK3588-DEVICE"]
}
```

#### Stop Running Test
```bash
curl -sX POST http://172.16.14.233:5001/api/test/stop | jq '.'
```

#### Check Test Status
```bash
curl -s http://172.16.14.233:5001/api/test/status | jq '.'
```

**Response:**
```json
{
  "running": true,
  "current_test": "CtsPermissionTestCases",
  "devices": ["RK3588-DEVICE"]
}
```

---

### 5. Real-Time Log Streaming

#### Stream Test Logs (Plain Text)
```bash
# Follow logs in real-time
curl -N http://172.16.14.233:5001/api/test/logs/stream
```

**⚠️ Important:** This endpoint returns **plain text**, not JSON!

Example output:
```
[10:39:00] ✅ SSH 连接成功
[10:39:01] 📤 上传文件：run_GMS_Test_Auto.sh
[10:39:02] 🚀 开始执行测试...
=== CtsPermissionTestCases ===
```

#### Download Current Log
```bash
curl -s http://172.16.14.233:5001/api/test/logs/download -o test.log
```

---

### 6. Test Reports & Results

#### List All Reports
```bash
curl -s http://172.16.14.233:5001/api/reports/list | jq '.'
```

**Response:**
```json
{
  "reports": [
    {
      "timestamp": "2026-04-05_10-39-00",
      "client_id": "hcq@ats-041055-64g",
      "test_type": "CTS",
      "result": "PASS"
    }
  ]
}
```

#### Get Report Files
```bash
curl -s "http://172.16.14.233:5001/api/reports/files/2026-04-05_10-39-00" | jq '.'
```

#### Analyze Report
```bash
curl -s "http://172.16.14.233:5001/api/reports/analyze/2026-04-05_10-39-00" | jq '.'
```

#### Download Report File
```bash
# Download specific report file
curl -O "http://172.16.14.233:5001/api/reports/download/2026-04-05_10-39-00/report.xml"
```

---

### 7. Device Operations

#### Lock Bootloader
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/bootloader-lock \
  -H "Content-Type: application/json" \
  -d '{"device_id": "RK3588-DEVICE"}' | jq '.'
```

#### Unlock Bootloader
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/bootloader-unlock \
  -H "Content-Type: application/json" \
  -d '{"device_id": "RK3588-DEVICE"}' | jq '.'
```

#### Check Bootloader Status
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/bootloader-status \
  -H "Content-Type: application/json" \
  -d '{"device_id": "RK3588-DEVICE"}' | jq '.'
```

#### Reboot Devices (Parallel)
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/reboot \
  -H "Content-Type: application/json" \
  -d '{"devices": ["DEVICE-1", "DEVICE-2"]}' | jq '.'
```

**Performance**: 10 devices in 3-5 seconds (vs 20-60 seconds before)

#### Remount as Read-Write (Parallel)
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/remount \
  -H "Content-Type: application/json" \
  -d '{"devices": ["DEVICE-1", "DEVICE-2"]}' | jq '.'
```

#### Connect to WiFi
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/connect-wifi \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "RK3588-DEVICE",
    "ssid": "TestWiFi",
    "password": "password123"
  }' | jq '.'
```

---

### 8. Network Diagnostics

#### Test Client-Host Connectivity
```bash
curl -sX POST http://172.16.14.233:5001/api/ssh/route/ping \
  -H "Content-Type: application/json" \
  -d '{
    "test_host_ip": "172.16.14.233",
    "client_ip": "172.16.14.68"
  }' | jq '.'
```

**Response:**
```json
{
  "success": true,
  "reachable": true,
  "latency": "<1ms (同一网段)",
  "same_network": true,
  "test_host_ip": "172.16.14.233",
  "client_ip": "172.16.14.68",
  "device_network": "172.16.21.0",
  "route_commands": {
    "linux": [
      "# 在测试主机上执行以下命令:",
      "# 添加到Android设备网段的路由（通过测试主机网关）",
      "sudo ip route add 172.16.21.0/24 via 172.16.14.1",
      "# 检查路由表: ip route show",
      "# 删除路由: sudo ip route del 172.16.21.0/24"
    ],
    "windows": [
      "# 在测试主机上执行以下命令:",
      "route add 172.16.21.0 mask 255.255.255.0 172.16.14.1",
      "# 检查路由表: route print",
      "# 删除路由: route delete 172.16.21.0"
    ]
  }
}
```

---

### 9. Client Information

#### Get Client IP
```bash
curl -s http://172.16.14.233:5001/api/client-info | jq '.'
```

**Response:**
```json
{
  "ip": "172.16.14.68"
}
```

#### Record Client Info
```bash
curl -sX POST http://172.16.14.233:5001/api/client-info \
  -H "Content-Type: application/json" \
  -d '{
    "ip": "172.16.14.68",
    "username": "hcq"
  }' | jq '.'
```

#### Auto-Detect Client Username
```bash
curl -sX POST http://172.16.14.233:5001/api/client-info/detect \
  -H "Content-Type: application/json" \
  -d '{
    "ip": "172.16.14.68",
    "username": "hcq",
    "password": "password"
  }' | jq '.'
```

---

### 10. Configuration Management

#### Get Current Config
```bash
# New endpoint
curl -s http://172.16.14.233:5001/api/config/read | jq '.'
```

#### Update Dynamic Config
```bash
# New endpoint
curl -sX POST http://172.16.14.233:5001/api/config/update \
  -H "Content-Type: application/json" \
  -d '{
    "device_host": "user@192.168.1.100",
    "device_pswd": "newpassword"
  }' | jq '.'
```

**Note:** Legacy `/api/config` endpoints (GET/POST) still work for backward compatibility.

**Updatable fields:** `device_host`, `device_pswd`, `client_hosts`, `client_ssh_credentials`, `ubuntu_user`, `ubuntu_host`, `ubuntu_pswd`, `local_server`, `suites_path`, `usbip_vid_pid`

---

### 11. VPN Management

#### Connect to VPN
```bash
curl -sX POST http://172.16.14.233:5001/api/vpn/connect | jq '.'
```

**Response:**
```json
{
  "success": true,
  "message": "VPN已连接",
  "connected": true
}
```

#### Disconnect VPN
```bash
curl -sX POST http://172.16.14.233:5001/api/vpn/disconnect | jq '.'
```

#### Check VPN Status
```bash
curl -s http://172.16.14.233:5001/api/vpn/status | jq '.'
```

**Response:**
```json
{
  "success": true,
  "connected": true,
  "server": "vpn.example.com"
}
```

---

### 12. File Management

#### Upload File
```bash
curl -sX POST http://172.16.14.233:5001/api/files/upload \
  -F "file=@/path/to/file.txt" \
  -F "file_path=/tmp" | jq '.'
```

#### List Files
```bash
curl -sX POST http://172.16.14.233:5001/api/files/list \
  -H "Content-Type: application/json" \
  -d '{"path": "/tmp"}' | jq '.'
```

#### Upload Files for Installation
```bash
curl -sX POST http://172.16.14.233:5001/api/files/install \
  -F "files=@/path/to/app.apk" | jq '.'
```

---

### 13. Firmware Burning

#### Burn Firmware to Device
```bash
curl -sX POST http://172.16.14.233:5001/api/burn/firmware \
  -F "firmware=@/path/to/firmware.img" \
  -F "device_id=DEVICE-123" | jq '.'
```

#### Burn GSI Image
```bash
curl -sX POST http://172.16.14.233:5001/api/burn/gsi \
  -F "gsi=@/path/to/system_gsi.img" \
  -F "device_id=DEVICE-123" | jq '.'
```

#### Burn Serial Number
```bash
curl -sX POST http://172.16.14.233:5001/api/burn/serial \
  -H "Content-Type: application/json" \
  -d '{"device_id": "DEVICE-123", "serial": "RK3588-SN001"}' | jq '.'
```

---

## Advanced Usage

### Parallel Testing on Multiple Devices
```bash
curl -sX POST http://172.16.14.233:5001/api/test/start \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["DEVICE-1", "DEVICE-2", "DEVICE-3"],
    "test_type": "CTS",
    "test_module": "CtsDeqpTestCases"
  }' | jq '.'
```

### Monitor Test Progress (Polling)
```bash
while true; do
  STATUS=$(curl -s http://172.16.14.233:5001/api/test/status)
  RUNNING=$(echo "$STATUS" | jq -r '.running')

  if [ "$RUNNING" = "false" ]; then
    echo "✅ Test completed"
    break
  fi

  CURRENT=$(echo "$STATUS" | jq -r '.current_test // "Unknown"')
  echo -ne "\r⏳ Running: $CURRENT ($(date '+%H:%M:%S')) "
  sleep 5
done
```

### Complete Test Workflow
```bash
#!/bin/bash
# Full test workflow example

DEVICE="RK3588-DEVICE"
TEST_TYPE="CTS"
TEST_MODULE="CtsPermissionTestCases"

# 1. Connect via USB/IP
echo "🔌 Connecting via USB/IP..."
curl -sX POST http://172.16.14.233:5001/api/usbip/start \
  -H "Content-Type: application/json" \
  -d '{"device_host": "user@windows-ip", "device_password": "password"}'
echo

# 2. Verify device is connected
echo "📱 Checking devices..."
curl -s http://172.16.14.233:5001/api/devices/list | jq '.'
echo

# 3. Start test
echo "🚀 Starting $TEST_TYPE test..."
curl -sX POST http://172.16.14.233:5001/api/test/start \
  -H "Content-Type: application/json" \
  -d "{\"devices\":[\"$DEVICE\"], \"test_type\":\"$TEST_TYPE\", \"test_module\":\"$TEST_MODULE\"}"
echo

# 4. Monitor progress
echo "⏳ Monitoring test..."
curl -s http://172.16.14.233:5001/api/test/logs/stream | tee test-output.log

# 5. Get results
echo "📊 Fetching latest report..."
curl -s http://172.16.14.233:5001/api/reports/list | jq '.reports[0]'
```

---

## Common Test Modules

| Test Type | Module Examples |
|-----------|----------------|
| **CTS** | `CtsPermissionTestCases`, `CtsDeqpTestCases`, `CtsUiTestCases` |
| **VTS** | `VtsHalLinuxV4L2V4l2Test`, `VtsKernelFilePermissionTest` |
| **GTS** | `GtsAssistSyncTestCases`, `GtsAssistantTestCases` |

---

## API Endpoints Summary

### System
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/system/health` | GET | Health check |
| `/api/system/docs` | GET | API documentation |
| `/api/help` | GET | API help |

### Device Management
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/devices/list` | GET | List connected devices |
| `/api/devices/info` | POST | Get device info (parallel) |
| `/api/devices/reboot` | POST | Reboot devices (parallel) |
| `/api/devices/remount` | POST | Remount RW (parallel) |
| `/api/devices/bootloader-lock` | POST | Lock bootloader |
| `/api/devices/bootloader-unlock` | POST | Unlock bootloader |
| `/api/devices/bootloader-status` | POST | Check bootloader status |
| `/api/devices/connect-wifi` | POST | Connect to WiFi |
| `/api/devices/management` | GET | Device management page |
| `/api/devices/user-locked` | GET | List user locks |

### Desktop VNC
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/desktop/vnc/start` | POST | Start VNC |
| `/api/desktop/vnc/stop` | POST | Stop VNC |
| `/api/desktop/vnc/status` | GET | Check VNC status |
| `/api/desktop/validate` | POST | Validate desktop host |

### USB/IP Connection
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/usbip/start` | POST | Start USB/IP connection |
| `/api/usbip/stop` | POST | Stop USB/IP connection |
| `/api/usbip/status` | GET | Check USB/IP status |
| `/api/usbip/auto-install` | POST | Auto-install usbipd |

### Test Execution
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/test/start` | POST | Start a test |
| `/api/test/stop` | POST | Stop running test |
| `/api/test/status` | GET | Get test status |
| `/api/test/logs/stream` | GET | Stream logs (plain text) |
| `/api/test/logs/download` | GET | Download current log |
| `/api/test/logs/list` | GET | List test logs |
| `/api/test/clean` | POST | Clean test logs |

### Reports
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/reports/list` | GET | List all reports |
| `/api/reports/files/{ts}` | GET | Get report files |
| `/api/reports/analyze/{ts}` | GET | Analyze report |
| `/api/reports/download/{ts}` | GET | Download report file |
| `/api/reports/delete` | DELETE | Delete report |

### Network & Client
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/client-info` | GET | Get client IP |
| `/api/client-info` | POST | Record client info |
| `/api/client-info/detect` | POST | Auto-detect username |
| `/api/ssh/route/ping` | POST | Test connectivity |
| `/api/ssh/route` | GET | Check SSH route |
| `/api/ssh/sshd-check` | GET | Check SSH server |
| `/api/ssh/sshd-install` | POST | Install SSH server |

### Configuration
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config/read` | GET | Get current config |
| `/api/config/update` | POST | Update dynamic config |
| `/api/config/validate` | GET | Validate config |
| `/api/config/values` | GET | Get config values |

### VPN Management
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/vpn/connect` | POST | Connect to VPN |
| `/api/vpn/disconnect` | POST | Disconnect VPN |
| `/api/vpn/status` | GET | Check VPN status |

### File Management
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/files/upload` | POST | Upload file |
| `/api/files/install` | POST | Upload files for install |
| `/api/files/progress` | GET | Get upload progress |
| `/api/files/list` | POST | List files |

### Firmware Burning
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/burn/firmware` | POST | Burn firmware |
| `/api/burn/gsi` | POST | Burn GSI image |
| `/api/burn/serial` | POST | Burn serial number |

### Legacy Endpoints (Backward Compatible)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config` | GET | Get config (legacy) |
| `/api/config` | POST | Update config (legacy) |

---

## Error Handling

All endpoints return JSON with a `success` field:

```bash
RESPONSE=$(curl -sX POST http://172.16.14.233:5001/api/test/start \
  -H "Content-Type: application/json" \
  -d '{"devices": ["DEVICE"], "test_type": "CTS", ...}')

if echo "$RESPONSE" | jq -e '.success' > /dev/null; then
  echo "✅ Success"
else
  ERROR=$(echo "$RESPONSE" | jq -r '.message // .detail // "Unknown error"')
  echo "❌ Error: $ERROR"
fi
```

---

## Performance Benchmarks

### Multi-Device Operations (10 devices)

| Operation | Before | After | Improvement |
|-----------|--------|-------|-------------|
| Device Info | 60-90s | 10-15s | **83% faster** |
| Reboot | 20-30s | 3-5s | **85% faster** |
| Remount | 40-60s | 10-15s | **75% faster** |

### Key Optimizations

1. **Parallel Execution** - All device operations run concurrently
2. **Optimized SSH Calls** - Reduced redundant connections
3. **Smart Caching** - Report analysis cached for faster repeat access
4. **Connection Pooling** - Reused SSH connections

---

## Practical Web Interface Workflows

### Workflow 1: Run CTS Test on Remote Device

1. **Open web interface**: http://172.16.14.233:5001
2. **Connect device** (if needed):
   - Click "📱 本地设备" to connect USB/IP device from Windows host
   - Enter Windows SSH password when prompted
   - Wait for device to appear in device list
3. **Select device**: Click device checkbox in ADB device list
4. **Configure test**:
   - Set **测试类型** to "CTS"
   - Set **测试模块** to "CtsPermissionTestCases"
   - Set **测试套件** to suite path (or use "📁 选择套件")
5. **Start test**: Click "▶ 开始测试"
6. **Monitor progress**: Watch real-time logs in log area
7. **View results**: Go to "📊 报告管理" page to see results

### Workflow 2: Check Network Route & Add Routing

1. **Click "📡 检查路由"** button in 操作控制
2. **Review connectivity status**:
   - Shows if client can reach test host
   - Displays latency and network information
   - Lists required route commands
3. **Add routing** (if needed):
   - Click "🖥️ 打开主机终端" button
   - Terminal page opens automatically
   - Route command is pre-filled: `sudo ip route add 172.16.21.0/24 via 172.16.14.1`
   - Press **Enter** to execute
   - Enter sudo password if prompted
4. **Verify**: Click "📡 检查路由" again to confirm connectivity

### Workflow 3: View Device Desktop via VNC

1. **Navigate to "🖥️ 主机桌面"** page
2. **Select host** from dropdown (if multiple hosts)
3. **View desktop**: VNC viewer loads automatically
4. **Control desktop**: Use mouse and keyboard in browser
5. **Add new host** (if needed):
   - Click "➕ 添加主机"
   - Enter host IP and SSH password
   - Click "添加并连接"

### Workflow 4: Upload and Install APK

1. **Select devices** in device list
2. **Drag APK file** to "📁 本地文件" drop zone
3. **Click "📤 上传到测试主机"**
4. **Wait for upload** (progress bar shows completion)
5. **Go to "🐧 主机终端"** page
6. **Install APK**:
   ```bash
   adb -s DEVICE_ID install -r /tmp/uploaded_app.apk
   ```

### Workflow 5: Analyze Test Report

1. **Go to "📈 报告分析"** page
2. **Upload report**:
   - Drag report XML/ZIP to upload zone, OR
   - Click "📤 上传报告" and select file
3. **View results**:
   - Summary cards show pass/fail statistics
   - Failure list shows detailed error information
4. **Copy failures**: Click failure case to copy
5. **Generate re-run command**: Failed cases can be used for re-testing

### Workflow 6: Terminal File Upload

1. **Go to "🐧 主机终端"** page
2. **Drag file** onto terminal area
3. **Overlay appears**: "📁 拖拽文件上传到主机/tmp"
4. **Drop file**: File uploads to `/tmp/` on test host
5. **Use file**: Access file in terminal at `/tmp/filename`

---

## Tips & Best Practices

### Web Interface Usage
1. **Device Locking**: Tests auto-lock devices; no manual locking needed for normal testing
2. **Route Check**: Always check routing before testing if client and device are on different networks
3. **Terminal Upload**: Use drag-and-drop for quick file uploads to test host
4. **VNC Access**: Use "🖥️ 主机桌面" for full GUI access to test host
5. **Report Analysis**: Upload old reports to "📈 报告分析" for detailed failure analysis
6. **Multi-User**: Check "👥 用户管理" to see which devices are locked by other users
7. **Device Info**: Use "📋 设备信息" to collect detailed specs before testing
8. **Parallel Testing**: Select multiple devices for simultaneous test execution

### API Automation Usage
1. **USB/IP Setup**: Ensure `usbipd` is installed on Windows host before connecting
2. **Log Streaming**: Use `/api/test/logs/stream` for real-time monitoring (plain text)
3. **Result Analysis**: Reports are stored with timestamps and accessible via `/api/reports/list`
4. **Performance**: Use batch device operations for 75-85% performance improvement
5. **Network Diagnostics**: Use `/api/ssh/route/ping` before testing to verify connectivity
6. **Config Management**: Use `/api/config/read` and `/api/config/update` endpoints
7. **Error Handling**: Always check `success` field in API responses
8. **Parallel Operations**: Send device arrays to batch endpoints for speed improvement

### Network & Connectivity
1. **Same Network**: If client and test host are on same network, no routing needed
2. **Different Networks**: Use "📡 检查路由" to diagnose and fix routing issues
3. **VPN**: Use VPN when accessing test host from external network
4. **USB/IP**: Use "📱 本地设备" when devices are connected to Windows machine
5. **Firewall**: Ensure ports 5001 (web), 22 (SSH), and 6080 (VNC) are accessible

### Performance Optimization
1. **Buffer Optimization**: Terminal uses O(n) buffer handling for smooth long sessions
2. **Parallel Device Ops**: 10 devices complete in 10-15s vs 60-90s before
3. **WebSocket Updates**: Real-time status without page refresh
4. **API Caching**: Report analysis cached for faster repeat access
5. **Connection Pooling**: SSH connections reused for multiple operations

---

## Troubleshooting

### Web Interface Issues

**Problem**: Devices not appearing in list
- **Solution**: Click "🔄 刷新设备" or check USB/IP connection status

**Problem**: Cannot connect to terminal
- **Solution**: Check SSH server status with "📡 检查SSHD"

**Problem**: VNC not showing desktop
- **Solution**: Click "🔄 刷新" on desktop page or verify VNC server is running

**Problem**: Route check shows unreachable
- **Solution**: Click "🖥️ 打开主机终端" and execute the suggested route command

**Problem**: File upload stuck
- **Solution**: Check network connectivity and verify file size (<100MB recommended)

### API Issues

**Problem**: API returns 401 Unauthorized
- **Solution**: Check authentication credentials and client IP is whitelisted

**Problem**: Device operations timeout
- **Solution**: Device may be offline; check device status with `/api/devices/list`

**Problem**: Test not starting
- **Solution**: Verify device is unlocked and not in use by another user

### Performance Issues

**Problem**: Slow device operations
- **Solution**: Use parallel batch operations instead of sequential calls

**Problem**: Terminal lag
- **Solution**: Clear terminal with "🧹 清空" - buffer may be large

**Problem**: Page load slow
- **Solution**: Check network latency to test host; use local network when possible

---

## System Requirements

### Web Browser
- **Modern browser**: Chrome 90+, Firefox 88+, Safari 14+, Edge 90+
- **JavaScript**: Enabled (required for WebSocket and xterm.js)
- **Network**: Stable connection to http://172.16.14.233:5001

### Test Host
- **OS**: Ubuntu 18.04+ with SSH access
- **Python**: Python 3.8+ with FastAPI
- **Android SDK**: Platform tools for ADB
- **VNC Server**: For desktop viewing (port 6080)
- **Disk Space**: 10GB+ for test suites and reports

### Windows Host (for USB/IP)
- **OS**: Windows 10/11 with SSH server enabled
- **USB/IP**: `usbipd-win` installed and running
- **Network**: Reachable from test host

### Android Devices
- **Android**: 8.0+ (API 26+) recommended
- **ADB**: USB debugging enabled
- **Network**: WiFi or Ethernet connectivity
- **Storage**: 5GB+ free space for test execution

---

## Interactive Documentation

For complete API documentation with try-it-out functionality:

**http://172.16.14.233:5001/docs** (Swagger UI)

**http://172.16.14.233:5001/api/help** (API Help)
