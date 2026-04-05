#!/bin/bash
# WebSocket快速测试脚本

echo "🔗 WebSocket连接测试"
echo "===================="
echo ""
echo "📍 连接到: ws://172.16.14.233:5001/api/system/websocket/test-client"
echo ""
echo "💡 连接成功后，脚本会自动发送ping消息"
echo "   您应该收到一个pong响应"
echo ""
echo "⌨️  按Ctrl+C退出连接"
echo ""
echo "开始连接..."
echo ""

# 使用expect来处理wscat的交互
if command -v expect >/dev/null 2>&1; then
    expect << 'EOF'
    set timeout 5
    spawn wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client
    expect ">"
    send "{\"type\":\"ping\"}\r"
    expect ">"
    send "{\"type\":\"refresh_devices\"}\r"
    expect {
        timeout { puts "\n⏱️  超时（5秒无响应），连接正常" }
        eof { puts "\n✅ 连接已关闭" }
    }
    interact
EOF
else
    echo "⚠️  expect未安装，使用简化模式"
    echo "连接后将自动发送ping消息，然后等待5秒..."
    echo ""

    # 使用管道方式发送消息
    (
        sleep 1
        echo '{"type":"ping"}'
        sleep 2
    ) | wscat -c ws://172.16.14.233:5001/api/system/websocket/test-client

    echo ""
    echo "✅ 测试完成"
fi
