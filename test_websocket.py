#!/usr/bin/env python3
"""
测试WebSocket连接的简单脚本
"""

import asyncio
import websockets
import json

async def test_websocket():
    uri = "ws://172.16.14.233:5001/api/system/websocket/test-client"

    try:
        print(f"🔗 连接到: {uri}")
        async with websockets.connect(uri) as websocket:
            print("✅ WebSocket连接成功")

            # 发送ping消息
            ping_msg = {"type": "ping"}
            await websocket.send(json.dumps(ping_msg))
            print(f"✅ 发送: {ping_msg}")

            # 接收响应
            response = await websocket.recv()
            print(f"✅ 收到: {response}")

            # 解析响应
            data = json.loads(response)
            if data.get('type') == 'pong':
                print("✅ 收到pong响应，连接正常！")
            else:
                print(f"⚠️  收到意外的响应类型: {data.get('type')}")

    except websockets.exceptions.InvalidStatusCode as e:
        print(f"❌ WebSocket连接失败: {e}")
        print("💡 提示: 请确认服务已启动并且端点路径正确")
    except Exception as e:
        print(f"❌ 错误: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket())
