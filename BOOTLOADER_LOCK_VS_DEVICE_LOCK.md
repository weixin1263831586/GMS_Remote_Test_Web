# Bootloader锁 vs 设备占用锁 - API重构说明

## 🎯 问题分析

原来的API命名和描述混乱，无法区分**Bootloader锁**和**设备占用锁**。

---

## 📊 两种锁的区别

| 特性 | Bootloader锁 | 设备占用锁 |
|------|-------------|-----------|
| **性质** | 硬件层面的系统锁定 | 软件层面的用户锁定 |
| **作用** | 控制设备能否刷入自定义系统 | 多用户环境下避免设备冲突 |
| **状态** | GREEN=锁定, ORANGE=未锁定 | 被/未被用户占用 |
| **持久性** | 永久（除非刷机） | 1小时自动过期 |
| **操作方式** | 物理按键+Fastboot | 软件API调用 |
| **危险性** | ⚠️ 高危操作 | ✅ 安全操作 |

---

## ✅ API重构方案

### 修改前（混乱）
```
POST /api/devices/lock          # 不清楚是什么锁
POST /api/devices/lock-status   # 不清楚是什么状态
GET  /api/devices/locks         # 不清楚是什么锁
```

### 修改后（清晰）
```
POST /api/devices/bootloader-lock      # 控制Bootloader锁
POST /api/devices/bootloader-unlock    # 解锁Bootloader（快捷方式）
POST /api/devices/bootloader-status    # 查询Bootloader锁状态
GET  /api/devices/locks               # 查询设备占用锁
```

---

## 🔐 Bootloader锁相关API

### 1. POST /api/devices/bootloader-lock
**用途**：控制设备Bootloader锁（锁定/解锁）

```bash
# 锁定Bootloader
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-lock" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RF8TC2W4JNH"],
    "action": "lock"
  }'

# 解锁Bootloader
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-lock" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RF8TC2W4JNH"],
    "action": "unlock"
  }'
```

### 2. POST /api/devices/bootloader-unlock
**用途**：解锁Bootloader（快捷方式，等同于bootliner-lock的unlock操作）

```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-unlock" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RF8TC2W4JNH"]
  }'
```

### 3. POST /api/devices/bootloader-status
**用途**：检查Bootloader锁状态

```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-status" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RF8TC2W4JNH"]
  }'
```

**响应示例**：
```json
{
  "success": true,
  "results": [
    {
      "device": "RF8TC2W4JNH",
      "locked": true,
      "state": "GREEN",
      "status": "已锁定"
    }
  ]
}
```

**状态说明**：
- `GREEN` = 已锁定（安全启动启用）
- `ORANGE` = 未锁定（可以刷机）
- `YELLOW` = 未锁定（可以刷机）

---

## 👥 设备占用锁相关API

### GET /api/devices/locks
**用途**：列出所有设备的占用状态（多用户环境）

```bash
curl -s "http://172.16.14.233:5001/api/devices/locks"
```

**响应示例**：
```json
{
  "success": true,
  "data": {
    "RF8TC2W4JNH": {
      "client_id": "hcq@172.16.14.68",
      "username": "hcq",
      "timestamp": "2026-04-04T15:30:00"
    }
  }
}
```

**解读**：
- `data` 为空 = 所有设备都空闲
- `data` 有内容 = 设备被占用，显示占用者信息

---

## 🎯 使用场景对比

### Bootloader锁 - 系统开发场景

**场景**：需要刷入自定义ROM

```bash
# 1. 检查当前状态
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-status" \
  -d '{"devices": ["RF8TC2W4JNH"]}'

# 2. 如果是GREEN（锁定），需要先解锁
curl -sX POST "http://172.16.14.233:5001/api/devices/bootliner-unlock" \
  -d '{"devices": ["RF8TC2W4JNH"]}'

# 3. 刷入自定义ROM
fastboot flash system custom_rom.img

# 4. 刷完后重新锁定
curl -sX POST "http://172.16.14.233:5001/api/devices/bootliner-lock" \
  -d '{"devices": ["RF8TC2W4JNH"], "action": "lock"}'
```

### 设备占用锁 - 多用户测试场景

**场景**：多人共用设备，避免冲突

```bash
# 用户A：查看可用设备
curl -s "http://172.16.14.233:5001/api/devices/locks" | jq ".data"

# 发现所有设备空闲，锁定一台设备开始测试
# （由系统自动锁定，无需手动调用）

# 用户B：想用设备，先查看占用状态
curl -s "http://172.16.14.233:5001/api/devices/locks" | jq ".data"

# 发现设备RF8TC2W4JNH被hcq占用，选择其他设备
```

---

## 📋 修改总结

### 重命名的API
- `/api/devices/lock` → `/api/devices/bootliner-lock`
- `/api/devices/lock-status` → `/api/devices/bootliner-status`

### 新增的API
- `/api/devices/bootliner-unlock` （独立的解锁端点）

### 保持不变的API
- `/api/devices/locks` （功能不变，只是描述更清晰）

### 改进点
1. ✅ **命名清晰** - 一眼就能看出是Bootloader锁还是占用锁
2. ✅ **功能独立** - 锁定和解锁有专门的端点
3. ✅ **描述准确** - 每个API的描述都明确说明了功能
4. ✅ **避免混淆** - 不会再有人搞混两种锁

---

## 💡 快速记忆

**Bootliner锁** = **硬件锁** = **系统安全**
- 关键词：`bootliner-*`
- 操作：刷机前需要解锁
- 危险性：⚠️ 高危

**设备占用锁** = **软件锁** = **用户管理**
- 关键词：`locks`
- 操作：多用户环境下的设备分配
- 危险性：✅ 安全

现在两种锁一目了然，不会再混淆了！🎉
