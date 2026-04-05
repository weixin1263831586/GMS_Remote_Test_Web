# VNC API参数修正说明

## ✅ 问题已修复

### 🐛 原问题：
`/api/vnc/start` 的API文档中错误地显示了 `device_id` 参数

### 🔧 修复方案：
移除不存在的 `device_id` 参数

---

## 📋 VNC启动API的正确用法

### POST /api/vnc/start
**用途**：启动VNC服务（远程桌面）

```bash
# 无参数启动（使用配置文件中的默认值）
curl -sX POST "http://172.16.14.233:5001/api/vnc/start"
```

**可选参数**（请求体）：
```json
{
  "host": "hcq@172.16.14.233",      // 可选：主机地址
  "password": "password",          // 可选：SSH密码
  "vnc_password": "vncpass"        // 可选：VNC密码
}
```

**响应示例**：
```json
{
  "success": true,
  "port": 5900
}
```

---

## 🔍 参数说明

### 实际的请求模型：
```python
class VNCStartRequest(BaseModel):
    """VNC启动请求"""
    host: Optional[str] = None        # 可选：主机地址
    password: Optional[str] = None    # 可选：SSH密码
    vnc_password: Optional[str] = None # 可选：VNC密码
```

### ❌ 错误的文档（已修复）：
```json
{
  "device_id": "VALUE"  // 这个参数根本不存在！
}
```

### ✅ 正确的文档：
```json
{}  // 无需参数，或提供可选的host/password/vnc_password
```

---

## 💡 使用场景

### 场景1：使用默认配置
```bash
# 直接启动，使用配置文件中的设置
curl -sX POST "http://172.16.14.233:5001/api/vnc/start"
```

### 场景2：指定主机信息
```bash
# 启动指定主机的VNC
curl -sX POST "http://172.16.14.233:5001/api/vnc/start" \
  -H "Content-Type: application/json" \
  -d '{
    "host": "hcq@172.16.14.233",
    "password": "mypassword",
    "vnc_password": "vncpass"
  }'
```

---

## 📝 VNC相关API

| API | 功能 | 参数 |
|-----|------|------|
| `POST /api/vnc/start` | 启动VNC | 无（可选host/password/vnc_password） |
| `POST /api/vnc/stop` | 停止VNC | 无 |
| `GET /api/vnc/status` | 查询状态 | 无 |

---

## 🎯 总结

**修复前**：
```bash
# 错误：文档显示需要device_id参数
curl -X POST "/api/vnc/start" -d '{"device_id": "VALUE"}'
```

**修复后**：
```bash
# 正确：无需参数，或提供可选的主机信息
curl -X POST "/api/vnc/start"

# 或提供可选参数
curl -X POST "/api/vnc/start" \
  -d '{"host": "hcq@172.16.14.233"}'
```

**请强制刷新浏览器页面**（`Ctrl + Shift + R`）查看修复后的API文档！

现在VNC启动API的参数描述已经正确了，不会再显示不存在的 `device_id` 参数了！🎉
