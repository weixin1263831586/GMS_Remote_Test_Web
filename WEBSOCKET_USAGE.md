# WebSocket 使用指南

## 端点信息

- **路径**: `/api/system/websocket/{client_id}`
- **分类**: 💚 系统管理
- **协议**: WebSocket
- **示例URL**: `ws://172.16.14.233:5001/api/system/websocket/hcq@172.16.14.68`

## 支持的消息类型

### 1. 心跳检测 (ping/pong)

**客户端发送**:
```json
{
  "type": "ping"
}
```

**服务器响应**:
```json
{
  "type": "pong",
  "timestamp": "2026-04-04T10:39:36.815555"
}
```

### 2. 刷新设备列表

**客户端发送**:
```json
{
  "type": "refresh_devices"
}
```

**服务器响应**: 最新的设备列表

### 3. 终端连接

**客户端发送**:
```json
{
  "type": "terminal_connect",
  "device_id": "RK3562GMS1",
  "mode": "ssh"  // 或 "adb"
}
```

### 4. 终端输入

**客户端发送**:
```json
{
  "type": "terminal_input",
  "input": "ls -la\n"
}
```

### 5. 终端调整大小

**客户端发送**:
```json
{
  "type": "terminal_resize",
  "rows": 24,
  "cols": 80
}
```

## 测试方法

### 方法1: 使用Python (推荐)

我们已经提供了测试脚本 `test_websocket.py`:

```bash
python3 test_websocket.py
```

### 方法2: 使用JavaScript (浏览器控制台)

```javascript
const ws = new WebSocket('ws://172.16.14.233:5001/api/system/websocket/test-client');

ws.onopen = () => {
    console.log('✅ WebSocket已连接');
    ws.send(JSON.stringify({ type: 'ping' }));
};

ws.onmessage = (event) => {
    console.log('✅ 收到消息:', event.data);
};

ws.onerror = (error) => {
    console.error('❌ WebSocket错误:', error);
};

ws.onclose = () => {
    console.log('🔌 WebSocket已关闭');
};
```

### 方法3: 安装wscat工具

如果您想使用wscat命令行工具，需要先安装Node.js和npm:

```bash
# 安装Node.js (Ubuntu/Debian)
sudo apt update
sudo apt install nodejs npm

# 使用npm安装wscat
sudo npm install -g wscat

# 测试连接
wscat -c ws://172.16.14.233:5001/api/system/websocket/YOUR_CLIENT_ID
```

### 方法4: 使用在线WebSocket测试工具

访问在线WebSocket测试网站，如:
- https://www.websocket.org/echo.html
- https://www.piesocket.com/websocket-tester

连接地址: `ws://172.16.14.233:5001/api/system/websocket/test-client`

## 验证结果

运行测试脚本后，您应该看到类似的输出:

```
🔗 连接到: ws://172.16.14.233:5001/api/system/websocket/test-client
✅ WebSocket连接成功
✅ 发送: {'type': 'ping'}
✅ 收到: {"type":"pong","timestamp":"2026-04-04T10:39:36.815555"}
✅ 收到pong响应，连接正常！
```

## 常见问题

**Q: 为什么wscat无法安装?**
A: wscat需要Node.js环境。建议使用Python或JavaScript进行测试。

**Q: 连接被拒绝(403)?**
A: 请检查client_id格式，应该是用户名@IP地址格式，如 `hcq@172.16.14.68`

**Q: 如何在Web应用中使用?**
A: 前端代码已经自动连接，无需手动配置。强制刷新浏览器页面即可。
