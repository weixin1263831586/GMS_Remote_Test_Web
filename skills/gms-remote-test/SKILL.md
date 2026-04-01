---
name: gms-remote-test
version: "2026.04.01-100000"
description: >-
  GMS Remote Test API Skill for FastAPI (Port 5001).
  Manage remote Android devices, run CTS/VTS/GTS tests via USB/IP or direct connection,
  and retrieve test results with real-time log streaming.
  **New**: Parallel device operations (75-85% faster), client info endpoints, route diagnostics.
---

# GMS Remote Test API Automation

Interact with the **GMS Auto Test FastAPI server (port 5001)** to remotely manage Android devices, run compatibility tests (CTS/VTS/GTS), and retrieve detailed test results.

## Quick Reference

| Item | Value |
|------|-------|
| **Server URL** | `http://172.16.14.233:5001` |
| **Interactive Docs** | http://172.16.14.233:5001/docs |
| **Skill Version** | `2026.04.01-100000` |
| **Performance** | 75-85% faster multi-device operations (parallel execution) |

---

## What's New (2026.04.01)

### Performance Optimizations
- ✅ **Parallel device operations** - 75-85% faster for multi-device tasks
- ✅ **Optimized device info collection** - 83% reduction in SSH calls
- ✅ **Cached XML analysis** - 90% faster for repeated report parsing

### New Endpoints
- ✅ `GET/POST /api/client-info` - Client information management
- ✅ `POST /api/client-info/detect` - Auto-detect client username
- ✅ `POST /api/ssh/route/ping` - Network connectivity diagnostics

---

## Core Features

### 1. Device Discovery & Management

#### List All Connected Devices
```bash
curl -s http://172.16.14.233:5001/api/devices | jq '.'
```

**Response format:**
```json
[
  {
    "device_id": "RK3588-DEVICE",
    "model": "Rockchip RK3588",
    "state": "device"
  }
]
```

#### Get Device Details (Optimized - Parallel)
```bash
curl -s "http://172.16.14.233:5001/api/devices/details/RK3588-DEVICE" | jq '.'
```

#### Get Device Info (New - Parallel Execution)
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/info \
  -H "Content-Type: application/json" \
  -d '{"devices": ["DEVICE-1", "DEVICE-2"]}' | jq '.'
```

**Performance**: 10 devices in 10-15 seconds (vs 60-90 seconds before)

#### Check Device Lock Status (New - Parallel)
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/lock-status \
  -H "Content-Type: application/json" \
  -d '{"devices": ["DEVICE-1", "DEVICE-2"]}' | jq '.'
```

---

### 2. USB/IP Remote Connection

Connect to Android devices hosted on a Windows machine via USB/IP tunneling.

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

### 3. Test Execution

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

### 4. Real-Time Log Streaming

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

#### Get Latest Logs (JSON)
```bash
curl -s http://172.16.14.233:5001/api/test/logs/latest | jq '.'
```

---

### 5. Test Reports & Results

#### List All Reports
```bash
curl -s http://172.16.14.233:5001/api/reports/list | jq '.reports[]'
```

**Response:**
```json
{
  "reports": [
    {
      "timestamp": "2026-03-31_10-39-00",
      "client_id": "hcq@ats-041055-64g",
      "test_type": "CTS",
      "result": "PASS"
    }
  ]
}
```

#### Get Report Files
```bash
curl -s "http://172.16.14.233:5001/api/reports/files/2026-03-31_10-39-00" | jq '.'
```

#### Download Report File
```bash
# Download specific report file
curl -O "http://172.16.14.233:5001/api/reports/download/2026-03-31_10-39-00/report.xml"
```

---

### 6. Device Operations

#### Lock Bootloader
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/lock \
  -H "Content-Type: application/json" \
  -d '{"device_id": "RK3588-DEVICE"}' | jq '.'
```

#### Unlock Bootloader
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/unlock \
  -H "Content-Type: application/json" \
  -d '{"device_id": "RK3588-DEVICE"}' | jq '.'
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

#### Remount as Read-Write (Optimized - Parallel)
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/remount \
  -H "Content-Type: application/json" \
  -d '{"devices": ["DEVICE-1", "DEVICE-2"]}' | jq '.'
```

#### Reboot Devices (Optimized - Parallel)
```bash
curl -sX POST http://172.16.14.233:5001/api/devices/reboot \
  -H "Content-Type: application/json" \
  -d '{"devices": ["DEVICE-1", "DEVICE-2"]}' | jq '.'
```

**Performance**: 10 devices in 3-5 seconds (vs 20-60 seconds before)

---

### 7. Network Diagnostics (New)

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

### 8. Client Information (New)

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

### 9. Configuration Management

#### Get Current Config
```bash
curl -s http://172.16.14.233:5001/api/config | jq '.'
```

#### Update Dynamic Config
```bash
curl -sX POST http://172.16.14.233:5001/api/config/update \
  -H "Content-Type: application/json" \
  -d '{
    "device_host": "user@192.168.1.100",
    "device_pswd": "newpassword"
  }' | jq '.'
```

**Updatable fields:** `device_host`, `device_pswd`, `client_hosts`, `client_ssh_credentials`, `ubuntu_user`, `ubuntu_host`, `ubuntu_pswd`, `local_server`, `suites_path`, `usbip_vid_pid`

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
curl -s http://172.16.14.233:5001/api/devices | jq '.'
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
| Lock Status | 20-30s | 3-5s | **85% faster** |
| Reboot | 20-30s | 3-5s | **85% faster** |
| Remount | 40-60s | 10-15s | **75% faster** |

### Key Optimizations

1. **Parallel Execution** - All device operations run concurrently
2. **Optimized SSH Calls** - Device info: 6 calls → 1 call (83% reduction)
3. **Smart Caching** - XML analysis cached for 90% faster repeat access
4. **Connection Pooling** - Reused SSH connections with context managers

---

## Helper Script

Source the helper script for convenient CLI access:

```bash
# Load helper functions
source /home/hcq/.claude/skills/gms-remote-test/scripts/gms-remote-test.sh

# Available commands:
gms-rt-help           # Show help
gms-rt-status         # Check server status
gms-rt-devices        # List devices
gms-rt-usbip-start    # Start USB/IP connection
gms-rt-usbip-stop     # Stop USB/IP connection
gms-rt-test-start     # Start a test
gms-rt-test-stop      # Stop running test
gms-rt-test-monitor   # Monitor test progress
gms-rt-stream-logs    # Stream logs in real-time
gms-rt-latest-report  # Get latest report
```

---

### 10. VPN Management (New)

#### Connect to Default VPN
```bash
curl -sX POST http://172.16.14.233:5001/api/vpn/connect | jq '.'
```

**No parameters required** - connects to default VPN configuration.

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

## API Endpoints Summary

### Device Management
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/devices` | GET | List connected devices |
| `/api/devices/details/{id}` | GET | Get device details |
| `/api/devices/info` | POST | Get device info (parallel) |
| `/api/devices/lock-status` | POST | Check lock status (parallel) |
| `/api/devices/reboot` | POST | Reboot devices (parallel) |
| `/api/devices/remount` | POST | Remount RW (parallel) |
| `/api/devices/lock` | POST | Lock bootloader |
| `/api/devices/unlock` | POST | Unlock bootloader |
| `/api/devices/connect-wifi` | POST | Connect to WiFi |

### USB/IP Connection
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/usbip/start` | POST | Start USB/IP connection |
| `/api/usbip/stop` | POST | Stop USB/IP connection |
| `/api/usbip/status` | GET | Check USB/IP status |

### Test Execution
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/test/start` | POST | Start a test |
| `/api/test/stop` | POST | Stop running test |
| `/api/test/status` | GET | Get test status |
| `/api/test/logs/stream` | GET | Stream logs (plain text) |
| `/api/test/logs/latest` | GET | Get latest logs (JSON) |

### Reports
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/reports/list` | GET | List all reports |
| `/api/reports/files/{ts}` | GET | Get report files |
| `/api/reports/download/{ts}/{filename}` | GET | Download report file |

### Network & Client
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/client-info` | GET | Get client IP |
| `/api/client-info` | POST | Record client info |
| `/api/client-info/detect` | POST | Auto-detect username |
| `/api/ssh/route/ping` | POST | Test connectivity |

### Configuration
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config` | GET | Get current config |
| `/api/config/update` | POST | Update dynamic config |

### VPN Management
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/vpn/connect` | POST | Connect to VPN (no params needed) |
| `/api/vpn/disconnect` | POST | Disconnect VPN |
| `/api/vpn/status` | GET | Check VPN status |

---

## Tips & Best Practices

1. **USB/IP Setup**: Ensure `usbipd` is installed on Windows host before connecting.
2. **Device Locking**: Tests auto-lock devices; no manual locking needed.
3. **Parallel Testing**: Run tests on multiple devices simultaneously for efficiency.
4. **Log Streaming**: Use `/api/test/logs/stream` for real-time monitoring (plain text).
5. **Result Analysis**: Reports are stored with timestamps and accessible via `/api/reports/list`.
6. **Performance**: Use batch device operations for 75-85% performance improvement.
7. **Network Diagnostics**: Use `/api/ssh/route/ping` before testing to verify connectivity.

---

## Interactive Documentation

For complete API documentation with try-it-out functionality:

**http://172.16.14.233:5001/docs** (Swagger UI)
