# 设备管理API参数修正完整报告

## 📋 修正的API列表

### ✅ 使用 `devices` 数组的API（支持批量操作）

| API | 端点 | 参数格式 |
|-----|------|---------|
| 设备锁定 | POST /api/devices/lock | `{"devices": ["xxx"], "action": "lock\|unlock"}` |
| 锁定状态 | POST /api/devices/lock-status | `{"devices": ["xxx"]}` |
| 设备信息 | POST /api/devices/info | `{"devices": ["xxx"]}` |
| 重启设备 | POST /api/devices/reboot | `{"devices": ["xxx"]}` |
| 重新挂载 | POST /api/devices/remount | `{"devices": ["xxx"]}` |
| WiFi连接 | POST /api/devices/connect-wifi | `{"devices": ["xxx"], "ssid": "...", "password": "..."}` |

### ✅ 使用 `serial_no` 的API（单设备操作）

| API | 端点 | 参数格式 |
|-----|------|---------|
| Shell命令 | POST /api/devices/shell | `{"serial_no": "xxx"}` |

---

## 🎯 正确使用示例

### 1. 获取设备信息
```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/info" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["c3d9b8674f4b94f6"]
  }' | jq "."
```

### 2. 重启设备
```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/reboot" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["c3d9b8674f4b94f6"]
  }' | jq "."
```

### 3. 连接WiFi
```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/connect-wifi" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["c3d9b8674f4b94f6"],
    "ssid": "AndroidWifi",
    "password": "1234567890"
  }' | jq "."
```

### 4. 打开Shell
```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/shell" \
  -H "Content-Type: application/json" \
  -d '{
    "serial_no": "c3d9b8674f4b94f6"
  }' | jq "."
```

---

## 🚀 批量操作示例

### 批量重启多个设备
```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/reboot" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": [
      "c3d9b8674f4b94f6",
      "RK3562GMS1",
      "RF8TC2W4JNH"
    ]
  }' | jq "."
```

**响应**:
```json
{
  "success": true,
  "results": [
    {"device": "c3d9b8674f4b94f6", "success": true},
    {"device": "RK3562GMS1", "success": true},
    {"device": "RF8TC2W4JNH", "success": true}
  ],
  "summary": {
    "total": 3,
    "success": 3,
    "failed": 0
  }
}
```

---

## 📝 修正详情

### 修正前 ❌
```json
{
  "device_id": "c3d9b8674f4b94f6"
}
```
**错误**: `{"detail": [{"type": "missing", "loc": ["body", "devices"], "msg": "Field required"}]}`

### 修正后 ✅
```json
{
  "devices": ["c3d9b8674f4b94f6"]
}
```
**成功**: `{"success": true, "results": [...], "summary": {...}}`

---

## 💡 设计说明

### 为什么使用数组？
1. **批量操作**: 支持同时操作多个设备
2. **统一接口**: 所有设备操作使用相同的参数格式
3. **灵活扩展**: 容易添加新的设备到操作列表

### 特殊API说明
- **/api/devices/shell**: 使用 `serial_no` 因为它是为终端会话准备的，一次只能连接一个设备
- **其他API**: 使用 `devices` 数组，支持批量操作

---

## ✅ 验证结果

所有API已测试通过：
- ✅ POST /api/devices/info - 正常工作
- ✅ POST /api/devices/reboot - 正常工作
- ✅ POST /api/devices/remount - 正常工作
- ✅ POST /api/devices/connect-wifi - 正常工作
- ✅ POST /api/devices/shell - 正常工作
- ✅ POST /api/devices/lock-status - 正常工作

**请强制刷新浏览器页面**（`Ctrl + Shift + R`）查看更新后的API文档！
