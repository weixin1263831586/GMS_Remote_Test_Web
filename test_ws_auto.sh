#!/bin/bash
# WebSocket自动化测试脚本

export PATH="$HOME/.local/bin:$PATH"

echo "🧪 WebSocket连接测试"
echo "===================="
echo ""

# 测试连接
echo "1️⃣  测试连接到: ws://172.16.14.233:5001/api/system/websocket/test-client"
echo ""

# 使用expect或其他方法来自动化wscat交互
# 这里使用一个简单的heredoc方法
(
  sleep 1
  echo '{"type": "ping"}'
  sleep 1
  echo '{"type": "refresh_devices"}'
  sleep 1
) | wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client 2>&1 | head -20

echo ""
echo "✅ 测试完成"
echo ""
echo "💡 提示: 如果上面显示连接成功和pong响应，说明WebSocket工作正常"
