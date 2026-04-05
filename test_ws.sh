#!/bin/bash
# WebSocket测试脚本

# 设置PATH以包含用户本地安装的wscat
export PATH="$HOME/.local/bin:$PATH"

echo "🔗 连接到 WebSocket..."
echo "   URL: ws://172.16.14.233:5001/api/system/websocket/test-client"
echo ""
echo "💡 连接成功后，您可以发送以下JSON消息进行测试："
echo "   {\"type\": \"ping\"}"
echo ""

# 启动wscat连接
wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client
