# WebSocket 交互式测试指南

## 🎯 快速测试

### 方法1: 运行自动化测试脚本

```bash
./wscat_quick_test.sh
```

这会自动连接并发送测试消息。

### 方法2: 手动交互式测试

**步骤1**: 连接到WebSocket（使用test-client作为测试ID）

```bash
wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client
```

**步骤2**: 等待连接成功提示

```
Connected (press CTRL+C to quit)
>
```

**步骤3**: 发送ping消息（复制下面的内容，粘贴到终端）

```json
{"type":"ping"}
```

**步骤4**: 查看服务器响应

```json
< {"type":"pong","timestamp":"2026-04-04T10:50:00.123456"}
```

**步骤5**: 测试其他消息

```json
{"type":"refresh_devices"}
```

**步骤6**: 按Ctrl+C退出连接

---

## 📋 常用测试消息

### 1. 心跳测试
```json
{"type":"ping"}
```

**预期响应**:
```json
{"type":"pong","timestamp":"2026-04-04T10:50:00.123456"}
```

### 2. 刷新设备列表
```json
{"type":"refresh_devices"}
```

**预期响应**: 返回当前连接的设备列表

### 3. 终端连接（需要设备ID）
```json
{"type":"terminal_connect","device_id":"RK3562GMS1","mode":"ssh"}
```

---

## 🔍 故障排除

### Q: 连接后立即断开

**A**: 这是正常的，因为：
- `YOUR_CLIENT_ID` 不是有效ID
- 需要使用有效的client_id，如：
  - `test-client` (用于测试)
  - `hcq@172.16.14.68` (您的真实客户端ID)

### Q: 连接成功但没有响应

**A**: 确保您：
1. 等待看到 `>` 提示符
2. 发送正确的JSON格式消息
3. 消息格式：`{"type":"ping"}`（注意引号）

### Q: 如何找到我的客户端ID？

**A**: 在Web浏览器中：
1. 打开 http://172.16.14.233:5001
2. 按F12打开开发者工具
3. 查看Console标签
4. 找到类似 `[WebSocket] Connected: hcq@172.16.14.68` 的消息

---

## 💡 使用技巧

### 技巧1: 使用test-client进行测试

```bash
wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client
```

这是最简单的测试方法，无需知道真实client_id。

### 技巧2: 批量测试消息

创建一个文件 `messages.txt`:
```json
{"type":"ping"}
{"type":"refresh_devices"}
```

然后使用：
```bash
cat messages.txt | wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client
```

### 技巧3: 保存会话日志

```bash
wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client 2>&1 | tee ws_session.log
```

---

## 📚 完整示例会话

```bash
$ wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client
Connected (press CTRL+C to quit)
> {"type":"ping"}
< {"type":"pong","timestamp":"2026-04-04T10:50:00.123456"}
> {"type":"refresh_devices"}
< {"type":"devices","devices":["RK3562GMS1"]}
> ^C%
```

---

## ✅ 验证清单

- [ ] wscat已安装 (`wscat --version`)
- [ ] 可以连接到WebSocket端点
- [ ] 发送ping消息后收到pong响应
- [ ] 可以发送其他类型的消息
- [ ] 按Ctrl+C可以正常退出

如果以上所有项都打勾，说明WebSocket功能完全正常！🎉
