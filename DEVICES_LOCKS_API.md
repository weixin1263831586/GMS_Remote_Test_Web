# /api/devices/locks API 说明

## 🎯 用途说明

### 📋 功能
**列出所有设备的锁定状态**（用于多用户环境下的设备管理）

### 🔒 什么是"设备锁定"？

在多用户测试环境中，为了避免多个用户同时操作同一台设备导致冲突，系统提供了**设备锁定机制**：

- 用户A锁定设备X → 其他用户无法操作设备X
- 用户A完成测试后解锁 → 其他用户可以操作设备X

### 📊 返回信息

```json
{
  "success": true,
  "data": {
    "RF8TC2W4JNH": {
      "client_id": "hcq@172.16.14.68",
      "username": "hcq",
      "timestamp": "2026-04-04T15:30:00"
    },
    "RK3562GMS1": {
      "client_id": "admin@172.16.14.100",
      "username": "admin",
      "timestamp": "2026-04-04T15:25:00"
    }
  }
}
```

**字段说明**：
- `client_id`: 锁定设备的客户端标识
- `username`: 锁定设备的用户名
- `timestamp`: 锁定时间

---

## 🎯 使用场景

### 1. 查看哪些设备被占用
```bash
$ curl -s "http://172.16.14.233:5001/api/devices/locks" | jq ".data"
```

**示例输出**：
```json
{
  "RF8TC2W4JNH": {
    "client_id": "hcq@172.16.14.68",
    "username": "hcq",
    "timestamp": "2026-04-04T15:30:00"
  }
}
```

**解读**：设备 `RF8TC2W4JNH` 被 `hcq@172.16.14.68` 锁定，其他人无法使用

### 2. 设备未被锁定（可用）
```json
{
  "success": true,
  "data": {}
}
```

**解读**：`data` 为空，表示当前没有设备被锁定，所有设备都可用

---

## 🔐 相关API

### 锁定/解锁设备

| API | 方法 | 说明 |
|-----|------|------|
| `/api/devices/lock` | POST | 锁定设备 |
| `/api/devices/lock-status` | POST | 检查锁定状态 |
| `/api/devices/locks` | GET | 列出所有锁定 |

### 锁定设备示例
```bash
# 锁定设备
curl -sX POST "http://172.16.14.233:5001/api/devices/lock" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RF8TC2W4JNH"],
    "action": "lock"
  }'

# 解锁设备
curl -sX POST "http://172.16.14.233:5001/api/devices/lock" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RF8TC2W4JNH"],
    "action": "unlock"
  }'
```

---

## 💡 实际应用场景

### 场景1：多用户测试环境

**团队环境**：
- 用户A正在使用设备RF8TC2W4JNH运行CTS测试
- 用户B想要使用同一台设备
- 用户B先调用 `/api/devices/locks` 查看哪些设备可用
- 发现RF8TC2W4JNH被锁定，选择其他设备

### 场景2：测试管理

**测试管理员**：
```bash
# 查看当前所有设备的锁定状态
curl -s "http://172.16.14.233:5001/api/devices/locks" | jq ".data"

# 输出显示哪些设备被哪些用户占用
# 可以据此分配测试任务
```

### 场景3：自动化测试

**测试脚本**：
```python
import requests

# 1. 查看可用设备
locks = requests.get("http://172.16.14.233:5001/api/devices/locks").json()
locked_devices = locks["data"].keys()

# 2. 获取所有设备
all_devices = requests.get("http://172.16.14.233:5001/api/devices/list").json()

# 3. 筛选未锁定的设备
available_devices = [d for d in all_devices if d["device_id"] not in locked_devices]

if available_devices:
    print(f"可用设备: {available_devices}")
else:
    print("所有设备都被占用，请等待")
```

---

## 🔍 特性

### 自动过期
- 锁定**1小时后自动过期**
- 防止设备被永久锁定
- 避免用户忘记解锁导致资源浪费

### 实时状态
- 返回当前的实时锁定状态
- 不包含历史记录

### 与Bootloader锁的区别

| 特性 | 设备锁定 (Device Lock) | Bootloader锁 |
|------|----------------------|-------------|
| 用途 | 多用户环境下的设备管理 | 设备启动验证状态 |
| 锁定对象 | 软件层面的用户锁定 | 硬件层面的系统锁定 |
| API | `/api/devices/locks` | `/api/devices/lock-status` |
| 时效 | 1小时自动过期 | 永久（除非刷机） |

---

## 📝 总结

**这个API的用途**：
1. ✅ 查看**哪些设备当前被占用**
2. ✅ 显示**被谁占用**（用户名、客户端ID）
3. ✅ 显示**占用时间**
4. ✅ 帮助**选择可用设备**进行测试

**适用场景**：
- 多用户测试环境
- 设备资源管理
- 自动化测试调度
- 测试任务分配

**与Bootloader锁无关** - 这不是查询硬件Bootloader状态的API！
