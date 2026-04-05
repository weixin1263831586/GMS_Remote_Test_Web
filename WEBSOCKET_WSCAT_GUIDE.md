# WebSocket 测试指南 - 使用 wscat

## ✅ wscat 已安装成功！

wscat 版本: 6.1.0
安装位置: ~/.local/bin/wscat

## 🚀 快速开始

### 方法1: 自动化测试（推荐）

```bash
./test_ws_auto.sh
```

### 方法2: 交互式测试

```bash
./test_ws.sh
```

连接成功后，您会看到提示符 `>`，然后可以输入JSON消息：

```json
{"type":"ping"}
```

您应该收到类似这样的响应：

```json
< {"type":"pong","timestamp":"2026-04-04T10:43:12.537451"}
```

### 方法3: 直接使用命令

```bash
# 在新的终端会话中（先执行 source ~/.bashrc 或重新打开终端）
wscat -c ws://172.16.14.233:5001/api/system/websocket/YOUR_CLIENT_ID
```

## 📋 可用的测试消息

### 1. 心跳测试
```json
{"type":"ping"}
```

### 2. 刷新设备列表
```json
{"type":"refresh_devices"}
```

### 3. 连接终端
```json
{"type":"terminal_connect","device_id":"RK3562GMS1","mode":"ssh"}
```

### 4. 发送终端命令
```json
{"type":"terminal_input","input":"ls -la\n"}
```

## 🧪 测试结果示例

```bash
$ ./test_ws_auto.sh
🧪 WebSocket连接测试
====================

1️⃣  测试连接到: ws://172.16.14.233:5001/api/system/websocket/test-client

> {"type":"pong","timestamp":"2026-04-04T10:43:12.537451"}
>

✅ 测试完成

💡 提示: 如果上面显示连接成功和pong响应，说明WebSocket工作正常
```

## 🔧 故障排除

### Q: 提示 "command not found: wscat"

**A**: 执行以下命令：
```bash
export PATH="$HOME/.local/bin:$PATH"
```

或者重新打开终端（PATH已自动配置）。

### Q: 连接被拒绝 (403)

**A**: 检查您的 client_id 格式，应该是：
- `test-client` (用于测试)
- 或 `username@ip` 格式，如 `hcq@172.16.14.68`

### Q: 连接超时

**A**: 确认服务正在运行：
```bash
curl http://172.16.14.233:5001/api/system/health
```

## 📚 更多信息

- WebSocket端点: `/api/system/websocket/{client_id}`
- 分类: 💚 系统管理
- 完整文档: 查看 `WEBSOCKET_USAGE.md`
- Python测试: 运行 `python3 test_websocket.py`

## 🎯 验证安装

```bash
$ wscat --version
6.1.0

$ which wscat
/home/hcq/.local/bin/wscat
```

如果看到上面的输出，说明wscat已正确安装！
