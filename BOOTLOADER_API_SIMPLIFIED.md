# Bootloader锁API简化说明

## ✅ 修改完成

### 🎯 问题：
`/api/devices/bootloader-lock` 需要传递 `action` 参数，但实际上已经有独立的解锁API了

### 🔧 解决方案：
移除 `action` 参数，简化API

---

## 📋 修改后的API

### 1. POST /api/devices/bootloader-lock
**用途**：锁定设备Bootloader

```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-lock" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RF8TC2W4JNH"]
  }'
```

**请求参数**：
- `devices` (array) - 设备序列号数组

**不再需要**：
- ~~`action`~~ - 已移除

### 2. POST /api/devices/bootloader-unlock
**用途**：解锁设备Bootloader

```bash
curl -sX POST "http://172.16.14.233:5001/api/devices/bootloader-unlock" \
  -H "Content-Type: application/json" \
  -d '{
    "devices": ["RF8TC2W4JNH"]
  }'
```

**请求参数**：
- `devices` (array) - 设备序列号数组

---

## 📊 对比

### 修改前（复杂）：
```bash
# 锁定（需要action参数）
curl -X POST "/api/devices/bootloader-lock" \
  -d '{"devices": ["xxx"], "action": "lock"}'

# 解锁（需要action参数）
curl -X POST "/api/devices/bootliner-lock" \
  -d '{"devices": ["xxx"], "action": "unlock"}'

# 或者使用独立的unlock
curl -X POST "/api/devices/bootliner-unlock" \
  -d '{"devices": ["xxx"]}'
```

### 修改后（简洁）：
```bash
# 锁定（无需action参数）
curl -X POST "/api/devices/bootloader-lock" \
  -d '{"devices": ["xxx"]}'

# 解锁（独立API）
curl -X POST "/api/devices/bootliner-unlock" \
  -d '{"devices": ["xxx"]}'
```

---

## 🎯 优势

1. **✅ 更简洁** - 不需要传递 `action` 参数
2. **✅ 更清晰** - API名称就说明了操作类型
3. **✅ 更一致** - 锁定和解锁分别有专门的端点
4. **✅ 更安全** - 避免误操作（不会因为action参数错误导致意外锁定/解锁）

---

## 💡 完整的Bootloader锁API

### 现在的设计：

| API | 功能 | 参数 |
|-----|------|------|
| `POST /api/devices/bootloader-lock` | 锁定Bootloader | `devices` |
| `POST /api/devices/bootliner-unlock` | 解锁Bootloader | `devices` |
| `POST /api/devices/bootliner-status` | 查询状态 | `devices` |

### 使用示例：

```bash
# 1. 查询状态
curl -sX POST "http://172.16.14.233:5001/api/devices/bootliner-status" \
  -d '{"devices": ["RF8TC2W4JNH"]}'

# 2. 如果未锁定，需要刷机则解锁
curl -sX POST "http://172.16.14.233:5001/api/devices/bootliner-unlock" \
  -d '{"devices": ["RF8TC2W4JNH"]}'

# 3. 刷机...
fastboot flash system custom_rom.img

# 4. 刷完后重新锁定
curl -sX POST "http://172.16.14.233:5001/api/devices/bootliner-lock" \
  -d '{"devices": ["RF8TC2W4JNH"]}'
```

---

## 📝 总结

**修改前**：一个API做两件事（需要action参数区分）
```bash
POST /api/devices/bootliner-lock?action=lock
POST /api/devices/bootliner-lock?action=unlock
```

**修改后**：两个API各做一件事（更清晰）
```bash
POST /api/devices/bootliner-lock      # 只负责锁定
POST /api/devices/bootliner-unlock    # 只负责解锁
```

**符合RESTful最佳实践**：一个端点只做一件事！🎯

---

## 🧪 测试验证

```bash
# 测试锁定API（无需action参数）
$ curl -sX POST "http://172.16.14.233:5001/api/devices/bootliner-lock" \
  -H "Content-Type: application/json" \
  -d '{"devices": ["test"]}' | jq ".success"
true

# 如果传递action参数会被忽略
$ curl -sX POST "http://172.16.14.233:5001/api/devices/bootliner-lock" \
  -H "Content-Type: application/json" \
  -d '{"devices": ["test"], "action": "unlock"}' | jq ".success"
true  # 仍然执行锁定操作，action参数被忽略
```

✅ 修改完成！API现在更简洁、更清晰了！
