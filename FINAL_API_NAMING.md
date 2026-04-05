# 设备锁API最终命名方案

## 🎯 最终命名方案

### Bootloader锁（硬件层面）
```
POST /api/devices/bootloader-lock      # 控制Bootloader锁（锁定/解锁）
POST /api/devices/bootloader-unlock    # 解锁Bootloader
POST /api/devices/bootloader-status    # 查询Bootloader锁状态
```

### 用户锁（软件层面）
```
GET /api/devices/user-locked           # 查询用户锁定设备
```

---

## ✅ 命名优势

### 1. 清晰区分
- `bootloader-*` = 硬件锁（系统安全）
- `user-locked` = 用户锁（设备占用）

### 2. 语义明确
- `user-locked` 一眼就知道是"用户层面的锁定"
- 不会和Bootloader锁混淆

### 3. 命名一致
- 都是形容词形式：`bootliner-*` vs `user-locked`
- 结构统一，易于理解

---

## 📊 完整对比表

| 特性 | Bootloader锁 | 用户锁 |
|------|-------------|--------|
| **API路径** | `/api/devices/bootloader-*` | `/api/devices/user-locked` |
| **性质** | 硬件锁 | 软件锁 |
| **层面** | 系统安全 | 用户管理 |
| **用途** | 控制刷机权限 | 多用户设备分配 |
| **持久性** | 永久（除非刷机） | 1小时自动过期 |
| **危险性** | ⚠️ 高危操作 | ✅ 安全操作 |

---

## 🧪 使用示例

### Bootloader锁操作

```bash
# 查询Bootloader状态
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-status" \
  -H "Content-Type: application/json" \
  -d '{"devices": ["RF8TC2W4JNH"]}'

# 锁定Bootloader
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-lock" \
  -H "Content-Type: application/json" \
  -d '{"devices": ["RF8TC2W4JNH"], "action": "lock"}'

# 解锁Bootloader
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-unlock" \
  -H "Content-Type: application/json" \
  -d '{"devices": ["RF8TC2W4JNH"]}'
```

### 用户锁查询

```bash
# 查询哪些设备被用户占用
curl -s "http://172.16.14.233:5001/api/devices/user-locked" | jq "."
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
- `data` 为空 → 所有设备都可用
- `data` 有内容 → 设备被占用，显示占用者信息

---

## 💡 命名演变历史

### 第一版（混乱）
```
POST /api/devices/lock          # 不清楚是什么锁
POST /api/devices/lock-status   # 不清楚是什么状态
GET  /api/devices/locks         # 不清楚是什么锁
```

### 第二版（部分改进）
```
POST /api/devices/bootloader-lock      # Bootloader锁
POST /api/devices/bootloader-status    # Bootliner状态
GET  /api/devices/locks               # 还是容易混淆
```

### 第三版（最终方案）✅
```
POST /api/devices/bootloader-lock      # Bootloader锁
POST /api/devices/bootloader-unlock    # Bootloader解锁
POST /api/devices/bootloader-status    # Bootliner状态
GET  /api/devices/user-locked          # 用户锁（清晰！）
```

---

## 🎯 快速记忆

**看到 `bootloader-*` → 硬件锁**
- 关键词：刷机、系统安全、高危操作

**看到 `user-locked` → 用户锁**
- 关键词：多用户、设备占用、安全操作

**再也不会混淆了！** 🎉

---

## 📝 测试验证

```bash
# 测试用户锁API
$ curl -s "http://172.16.14.233:5001/api/devices/user-locked"
{
  "success": true,
  "data": {}
}

# 确认旧API已删除
$ curl -s "http://172.16.14.233:5001/api/devices/locks"
{"detail":"Not Found"}
```

✅ 修改完成并验证通过！
